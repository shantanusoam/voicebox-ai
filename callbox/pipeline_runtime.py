"""Provider-neutral low-cost voice runtime.

Unlike the native OpenAI Realtime path, this runtime owns turn detection locally
and composes three replaceable components:

    PCM audio -> STT -> tool-calling LLM -> TTS -> PCM audio

The model still never owns business data. Every tool call crosses the same
``Orchestrator`` policy boundary used by the native realtime runtime.

This is intentionally a POC media runtime, not a claim of production full-duplex
quality. The input reader stays live while a response is generated so caller
speech can clear queued playback; upstream STT/LLM/TTS requests themselves are
not cancellable once sent.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime
import json
import time
from collections import deque

from .audio import pcm_rms
from .component_providers import build_llm, build_stt, build_tts
from .db import IST
from .errors import AppError
from .orchestrator import Orchestrator, ToolContext


class LocalTurnDetector:
    """Small deterministic energy VAD/endpointer for PCM16 mono audio.

    The threshold is deliberately configurable because browser microphones and
    8 kHz telephone legs have very different noise floors. It keeps a short
    pre-roll so initial consonants are not lost when the threshold is crossed.
    """

    def __init__(self, rate: int, threshold: int, silence_ms: int,
                 max_turn_seconds: int, pre_roll_ms: int = 180):
        self.rate = rate
        self.threshold = threshold
        self.bytes_per_ms = rate * 2 / 1000
        self.silence_bytes = int(self.bytes_per_ms * silence_ms)
        self.max_bytes = rate * 2 * max_turn_seconds
        self.pre_roll_bytes = int(self.bytes_per_ms * pre_roll_ms)
        self.pre_roll = bytearray()
        self.turn = bytearray()
        self.trailing_silence = 0
        self.active = False

    def _remember(self, pcm: bytes):
        self.pre_roll.extend(pcm)
        if len(self.pre_roll) > self.pre_roll_bytes:
            del self.pre_roll[:-self.pre_roll_bytes]

    def feed(self, pcm: bytes) -> tuple[bool, bytes | None]:
        """Return ``(speech_started, completed_turn)`` for one PCM chunk."""
        if not pcm:
            return False, None
        speech = pcm_rms(pcm) >= self.threshold
        started = False

        if not self.active:
            if not speech:
                self._remember(pcm)
                return False, None
            started = True
            self.active = True
            self.turn.extend(self.pre_roll)
            self.pre_roll.clear()
            self.turn.extend(pcm)
            self.trailing_silence = 0
        else:
            self.turn.extend(pcm)
            if speech:
                self.trailing_silence = 0
            else:
                self.trailing_silence += len(pcm)

        if len(self.turn) >= self.max_bytes or self.trailing_silence >= self.silence_bytes:
            return started, self.finish()
        return started, None

    def finish(self) -> bytes | None:
        if not self.active:
            return None
        audio = bytes(self.turn)
        self.turn.clear()
        self.trailing_silence = 0
        self.active = False
        self.pre_roll.clear()
        return audio or None

    def reset(self):
        self.pre_roll.clear()
        self.turn.clear()
        self.trailing_silence = 0
        self.active = False


class PipelineSession:
    """One call using replaceable STT, LLM and TTS components."""

    MAX_TOOL_ROUNDS = 6
    HISTORY_TURNS = 8

    def __init__(self, db, agent, config, workspace, call_id, channel='browser',
                 orchestrator=None, stt=None, llm=None, tts=None):
        self.db, self.agent, self.config = db, agent, config
        self.workspace, self.call_id = workspace, call_id
        self.orchestrator = orchestrator or Orchestrator(db, config)
        self.context = ToolContext(workspace=workspace, call_id=call_id, channel=channel)
        self.stt = stt or build_stt(config)
        self.llm = llm or build_llm(config)
        self.tts = tts or build_tts(config)
        self.turns: deque[list[dict]] = deque(maxlen=self.HISTORY_TURNS)
        self.tool_log = []
        self.started = time.monotonic()
        self.generation = 0

    def settings(self):
        return self.db.settings(self.workspace)

    def expired(self):
        return time.monotonic() - self.started > self.config.realtime_max_seconds

    def system_prompt(self) -> str:
        from .realtime import INSTRUCTIONS, LANGUAGE_RULES
        settings = self.settings()
        return INSTRUCTIONS.format(
            business=settings.get('name', 'this business'),
            language_rule=LANGUAGE_RULES.get(settings.get('language', 'en'),
                                             LANGUAGE_RULES['en']),
            persona=settings.get('persona') or '',
            today=datetime.now(IST).strftime('%A, %d %B %Y'))

    def language_hints(self) -> tuple[str | None, str]:
        language = self.settings().get('language', 'en')
        if language == 'en':
            return 'en', 'en-IN'
        if language == 'hi':
            return 'hi', 'hi-IN'
        return None, self.config.sarvam_language

    def messages(self, current: list[dict]) -> list[dict]:
        history = [message for block in self.turns for message in block]
        return [{'role': 'system', 'content': self.system_prompt()}, *history, *current]

    async def dispatch(self, name, arguments):
        result = await self.orchestrator.execute(name, arguments or {}, self.context)
        self.tool_log.append({'tool': name, 'ok': not result.get('error'), 'result': result})
        return result

    async def answer(self, transcript: str, generation: int, emit) -> str:
        """Run the tool-calling loop and return authoritative reply text."""
        current = [{'role': 'user', 'content': transcript}]
        for _ in range(self.MAX_TOOL_ROUNDS):
            completion = await self.llm.complete(
                self.messages(current), self.orchestrator.published(self.context.phase))
            calls = completion.get('tool_calls') or []
            text = (completion.get('text') or '').strip()

            if calls:
                current.append(completion['assistant_message'])
                for call in calls:
                    before = self.context.phase
                    result = await self.dispatch(call['name'], call['arguments'])
                    await emit({'type': 'tool', 'tool': call['name'], 'result': result})
                    if self.context.phase != before:
                        await emit({'type': 'phase', 'phase': self.context.phase})
                    current.append({
                        'role': 'tool',
                        'tool_call_id': call['id'],
                        'content': json.dumps(result, default=str),
                    })
                continue

            if not text:
                raise AppError(502, 'provider_format',
                               'Pipeline LLM returned neither speech nor a tool call.')
            current.append({'role': 'assistant', 'content': text})
            self.turns.append(current)
            return text

        raise AppError(502, 'tool_loop_limit',
                       'The agent reached its tool-call safety limit. Nothing further was changed.')

    async def process_turn(self, pcm: bytes, generation: int, emit):
        stt_language, tts_language = self.language_hints()
        transcript = await self.stt.transcribe(pcm, self.config.realtime_rate, stt_language)
        if generation != self.generation:
            return
        self.db.message(self.call_id, 'caller', transcript)
        await emit({'type': 'caller.said', 'text': transcript})

        reply = await self.answer(transcript, generation, emit)
        self.db.message(self.call_id, 'assistant', reply)
        await emit({'type': 'assistant.delta', 'text': reply})
        await emit({'type': 'assistant.done', 'text': reply})

        if generation != self.generation:
            return
        audio = await self.tts.speak(reply, self.config.realtime_rate, tts_language)
        if generation != self.generation:
            return
        await emit_pcm(audio, self.config.realtime_rate, generation, self, emit)
        if generation == self.generation:
            await emit({'type': 'turn.done'})


async def emit_pcm(pcm: bytes, rate: int, generation: int,
                   session: PipelineSession, emit):
    """Emit 20 ms PCM chunks; phone pacing remains in AudioSocketLeg."""
    frame_bytes = max(2, rate * 2 * 20 // 1000)
    for offset in range(0, len(pcm), frame_bytes):
        if generation != session.generation:
            return
        chunk = pcm[offset:offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += bytes(frame_bytes - len(chunk))
        await emit({'type': 'audio', 'pcm16': base64.b64encode(chunk).decode()})
        await asyncio.sleep(0)


async def _reader(socket, session: PipelineSession, detector: LocalTurnDetector,
                  queue: asyncio.Queue, emit):
    while True:
        raw = await socket.receive_text()
        if len(raw) > 2_000_000:
            raise AppError(413, 'frame_size', 'Audio frame too large.')
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        kind = message.get('type')
        if kind == 'audio':
            encoded = message.get('pcm16')
            if not isinstance(encoded, str) or not encoded:
                continue
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except Exception:
                continue
            started, turn = detector.feed(pcm)
            if started:
                session.generation += 1
                await emit({'type': 'playback.clear'})
            if turn:
                await queue.put((session.generation, turn))
        elif kind == 'interrupt':
            detector.reset()
            session.generation += 1
            await emit({'type': 'playback.clear'})
        elif kind == 'end':
            tail = detector.finish()
            if tail:
                await queue.put((session.generation, tail))
            await queue.put(None)
            return
        if session.expired():
            raise AppError(409, 'realtime_expired', 'Conversation time limit reached.')


async def _worker(session: PipelineSession, queue: asyncio.Queue, emit):
    while True:
        item = await queue.get()
        if item is None:
            return
        generation, pcm = item
        if generation != session.generation:
            continue
        try:
            await session.process_turn(pcm, generation, emit)
        except AppError as error:
            await emit({'type': 'error', 'code': error.code, 'message': error.message})
        except Exception:
            await emit({'type': 'error', 'code': 'pipeline_error',
                        'message': 'The voice pipeline could not complete that turn.'})


async def _speak_greeting(session: PipelineSession, greeting: str, emit):
    generation = session.generation
    try:
        _, tts_language = session.language_hints()
        audio = await session.tts.speak(greeting, session.config.realtime_rate, tts_language)
        if generation != session.generation:
            return
        await emit({'type': 'assistant.delta', 'text': greeting})
        await emit({'type': 'assistant.done', 'text': greeting})
        await emit_pcm(audio, session.config.realtime_rate, generation, session, emit)
    except AppError as error:
        await emit({'type': 'error', 'code': error.code, 'message': error.message})


async def run(socket, db, agent, config, workspace, call_id, greeting,
              channel='browser', orchestrator=None):
    """Own one provider-neutral pipeline conversation until hangup."""
    if not config.pipeline_available:
        raise AppError(503, 'pipeline_not_configured',
                       'Configure credentials for the selected STT, LLM and TTS providers.')

    session = PipelineSession(db, agent, config, workspace, call_id, channel, orchestrator)
    detector = LocalTurnDetector(config.realtime_rate, config.pipeline_vad_rms,
                                 config.pipeline_silence_ms, config.pipeline_max_turn_seconds)
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    send_lock = asyncio.Lock()

    async def emit(payload):
        async with send_lock:
            await socket.send_json(payload)

    await emit({
        'type': 'ready', 'call_id': call_id, 'rate': config.realtime_rate,
        'model': ('pipeline:' + config.pipeline_stt_provider + '/' +
                  config.pipeline_llm_provider + '/' + config.pipeline_tts_provider),
        'hardware_verified': False,
    })

    reader = asyncio.create_task(_reader(socket, session, detector, queue, emit))
    worker = asyncio.create_task(_worker(session, queue, emit))
    greeting_task = asyncio.create_task(_speak_greeting(session, greeting, emit))
    done, pending = await asyncio.wait({reader, worker}, return_when=asyncio.FIRST_COMPLETED)

    if reader in done:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(worker, timeout=5)
    for task in pending | {greeting_task}:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    for task in done:
        with contextlib.suppress(asyncio.CancelledError):
            task.result()

    session.orchestrator.release(call_id)
    return session.tool_log
