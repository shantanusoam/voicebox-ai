"""Provider-neutral components for the low-cost voice pipeline.

This module intentionally separates speech recognition, the tool-calling LLM,
and speech synthesis.  The business agent and telephony layer never need to
know whether a turn used Groq, Sarvam, OpenAI or OpenRouter.

All credentials stay server-side.  Tests inject ``httpx.MockTransport`` and
never make paid requests.
"""
from __future__ import annotations

from array import array
import base64
import binascii
import io
import json
import sys
import wave

import httpx

from .audio import pcm_rms, to_wav
from .errors import AppError


MAX_AUDIO_BYTES = 4 * 1024 * 1024


def _provider_error(status: int) -> AppError:
    if status == 429:
        return AppError(503, 'provider_rate_limit',
                        'Voice provider is busy or its quota is exhausted. No retry was made automatically.')
    return AppError(502, 'provider_error',
                    f'Voice provider rejected the request (HTTP {status}). Check server-side configuration.')


async def _post(client: httpx.AsyncClient, route: str, **kwargs) -> httpx.Response:
    try:
        response = await client.post(route, **kwargs)
    except httpx.TimeoutException:
        raise AppError(504, 'provider_timeout', 'Voice provider timed out. No automatic retry was made.')
    except httpx.RequestError:
        raise AppError(502, 'provider_unavailable', 'Cannot reach the configured voice provider.')
    if response.status_code >= 400:
        raise _provider_error(response.status_code)
    return response


def _require_audio(pcm: bytes, sample_rate: int, max_seconds: int = 30) -> None:
    if sample_rate < 8000 or sample_rate > 48000:
        raise AppError(422, 'audio_rate', 'Unsupported PCM sample rate.')
    if not pcm or len(pcm) > MAX_AUDIO_BYTES:
        raise AppError(413, 'audio_size', 'Audio turn is empty or too large.')
    if len(pcm) / (sample_rate * 2) > max_seconds:
        raise AppError(413, 'audio_duration', f'Audio turn exceeds the {max_seconds}-second limit.')
    if pcm_rms(pcm) < 80:
        raise AppError(422, 'no_speech', 'No speech was detected in this turn.')


def _pcm16_from_wav(wav_bytes: bytes, target_rate: int) -> bytes:
    """Decode mono PCM16 WAV and resample to ``target_rate``.

    The project already uses a simple laboratory linear resampler. Keep the
    same quality boundary here: adequate for a POC, not a production DSP claim.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as src:
            if src.getnchannels() != 1 or src.getsampwidth() != 2:
                raise AppError(502, 'tts_format', 'Expected mono PCM16 WAV from the speech provider.')
            rate = src.getframerate()
            if not 8000 <= rate <= 48000:
                raise AppError(502, 'tts_format', 'Speech provider returned an unsupported sample rate.')
            data = src.readframes(src.getnframes())
    except (wave.Error, EOFError):
        raise AppError(502, 'tts_format', 'Speech provider returned an invalid WAV payload.')

    if rate == target_rate:
        return data
    source = array('h')
    source.frombytes(data[:len(data) - len(data) % 2])
    if sys.byteorder != 'little':
        source.byteswap()
    if len(source) < 2:
        return b''
    count = int(len(source) * target_rate / rate)
    ratio = rate / target_rate
    last = len(source) - 1
    output = array('h', [0]) * count
    for n in range(count):
        index = n * ratio
        left = int(index)
        if left >= last:
            output[n] = source[last]
            continue
        value = source[left] + (source[left + 1] - source[left]) * (index - left)
        output[n] = max(-32768, min(32767, round(value)))
    if sys.byteorder != 'little':
        output.byteswap()
    return output.tobytes()


class GroqSTT:
    """Cheap multilingual Whisper transcription through Groq."""

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport

    def client(self):
        if not self.config.groq_key:
            raise AppError(503, 'provider_not_configured', 'Set GROQ_API_KEY to use Groq transcription.')
        return httpx.AsyncClient(
            base_url='https://api.groq.com/openai/v1/',
            headers={'Authorization': 'Bearer ' + self.config.groq_key},
            timeout=httpx.Timeout(35, connect=10), transport=self.transport)

    async def transcribe(self, pcm: bytes, sample_rate: int, language_code: str | None = None) -> str:
        _require_audio(pcm, sample_rate, self.config.pipeline_max_turn_seconds)
        wav = to_wav(pcm, sample_rate)
        data = {
            'model': self.config.groq_stt_model,
            'response_format': 'json',
            'temperature': '0',
        }
        if language_code in {'en', 'hi'}:
            data['language'] = language_code
        async with self.client() as client:
            response = await _post(client, 'audio/transcriptions',
                                   files={'file': ('turn.wav', wav, 'audio/wav')}, data=data)
        try:
            text = str(response.json()['text']).strip()
        except (ValueError, KeyError, TypeError):
            raise AppError(502, 'provider_format', 'Groq returned an invalid transcription response.')
        if not text:
            raise AppError(422, 'no_speech', 'No speech was recognized in this turn.')
        if len(text) > 3000:
            raise AppError(422, 'transcript_too_long', 'Transcription exceeded the turn limit.')
        return text


class SarvamSTT:
    """India-first Saaras REST transcription."""

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport

    def client(self):
        if not self.config.sarvam_key:
            raise AppError(503, 'provider_not_configured', 'Set SARVAM_API_KEY to use Saaras transcription.')
        return httpx.AsyncClient(
            base_url='https://api.sarvam.ai/',
            headers={'api-subscription-key': self.config.sarvam_key},
            timeout=httpx.Timeout(35, connect=10), transport=self.transport)

    async def transcribe(self, pcm: bytes, sample_rate: int, language_code: str | None = None) -> str:
        _require_audio(pcm, sample_rate, self.config.pipeline_max_turn_seconds)
        wav = to_wav(pcm, sample_rate)
        data = {
            'model': self.config.sarvam_stt_model,
            'language_code': language_code if language_code and '-' in language_code else 'unknown',
            'with_timestamps': 'false',
        }
        if self.config.sarvam_stt_model == 'saaras:v3' and self.config.sarvam_stt_mode:
            data['mode'] = self.config.sarvam_stt_mode
        async with self.client() as client:
            response = await _post(client, 'speech-to-text',
                                   files={'file': ('turn.wav', wav, 'audio/wav')}, data=data)
        try:
            text = str(response.json()['transcript']).strip()
        except (ValueError, KeyError, TypeError):
            raise AppError(502, 'provider_format', 'Sarvam returned an invalid transcription response.')
        if not text:
            raise AppError(422, 'no_speech', 'No speech was recognized in this turn.')
        if len(text) > 3000:
            raise AppError(422, 'transcript_too_long', 'Transcription exceeded the turn limit.')
        return text


class SarvamTTS:
    speech_mime = 'audio/wav'

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport

    def client(self):
        if not self.config.sarvam_key:
            raise AppError(503, 'provider_not_configured', 'Set SARVAM_API_KEY to use Bulbul speech.')
        return httpx.AsyncClient(
            base_url='https://api.sarvam.ai/',
            headers={'api-subscription-key': self.config.sarvam_key},
            timeout=httpx.Timeout(45, connect=10), transport=self.transport)

    async def speak(self, text: str, target_rate: int, language_code: str | None = None) -> bytes:
        text = str(text).strip()
        if not text or len(text) > 2400:
            raise AppError(422, 'speech_length', 'Speech text must contain 1 to 2400 characters.')
        body = {
            'text': text,
            'language_code': language_code or self.config.sarvam_language,
            'speaker': self.config.sarvam_voice,
            'model': self.config.sarvam_tts_model,
            'output_audio_codec': 'wav',
            'speech_sample_rate': self.config.sarvam_tts_rate,
            'pace': 1.0,
        }
        if self.config.sarvam_tts_model == 'bulbul:v3':
            body['temperature'] = 0.45
        async with self.client() as client:
            response = await _post(client, 'text-to-speech', json=body)
        try:
            audios = response.json()['audios']
            if not isinstance(audios, list) or not audios:
                raise ValueError
            wav = base64.b64decode(''.join(str(part) for part in audios), validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise AppError(502, 'provider_format', 'Sarvam returned invalid generated speech.')
        if len(wav) > MAX_AUDIO_BYTES:
            raise AppError(502, 'speech_size', 'Generated speech exceeded the playback limit.')
        return _pcm16_from_wav(wav, target_rate)


class OpenAISpeechComponents:
    """Adapter around the existing OpenAI turn-based provider."""

    def __init__(self, config, transport=None):
        from .providers import OpenAIProvider
        self.provider = OpenAIProvider(config, transport=transport)

    async def transcribe(self, pcm: bytes, sample_rate: int, language_code: str | None = None) -> str:
        _require_audio(pcm, sample_rate, 30)
        return await self.provider.transcribe(to_wav(pcm, sample_rate), 'audio/wav')

    async def speak(self, text: str, target_rate: int, language_code: str | None = None) -> bytes:
        wav = await self.provider.speak(text, wav=True)
        return _pcm16_from_wav(wav, target_rate)


class ToolCallingLLM:
    """OpenAI-compatible chat-completions tool loop for OpenAI/OpenRouter."""

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport
        self.last_cost_usd = 0.0

    def client(self):
        provider = self.config.pipeline_llm_provider
        if provider == 'openrouter':
            if not self.config.openrouter_key:
                raise AppError(503, 'provider_not_configured',
                               'Set OPENROUTER_API_KEY to use the pipeline LLM.')
            return httpx.AsyncClient(
                base_url='https://openrouter.ai/api/v1/',
                headers={'Authorization': 'Bearer ' + self.config.openrouter_key,
                         'X-Title': 'CallBox Voice Pipeline'},
                timeout=httpx.Timeout(45, connect=10), transport=self.transport)
        if provider == 'openai':
            if not self.config.api_key:
                raise AppError(503, 'provider_not_configured',
                               'Set OPENAI_API_KEY to use the pipeline LLM.')
            return httpx.AsyncClient(
                base_url='https://api.openai.com/v1/',
                headers={'Authorization': 'Bearer ' + self.config.api_key},
                timeout=httpx.Timeout(45, connect=10), transport=self.transport)
        raise AppError(503, 'provider_not_configured', 'Unsupported pipeline LLM provider.')

    @staticmethod
    def _tools(tools):
        return [{'type': 'function', 'function': {
            'name': item['name'],
            'description': item.get('description', ''),
            'parameters': item.get('parameters', {'type': 'object', 'properties': {}}),
        }} for item in tools]

    @staticmethod
    def _content(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ''.join(str(part.get('text', '')) for part in value if isinstance(part, dict))
        return ''

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            'model': self.config.pipeline_llm_model,
            'messages': messages,
            'tools': self._tools(tools),
            'tool_choice': 'auto',
            'temperature': 0.15,
            'max_tokens': 350,
        }
        async with self.client() as client:
            response = await _post(client, 'chat/completions', json=body)
        try:
            payload = response.json()
            message = payload['choices'][0]['message']
        except (ValueError, KeyError, IndexError, TypeError):
            raise AppError(502, 'provider_format', 'Pipeline LLM returned an invalid response.')

        raw_cost = ((payload.get('usage') or {}).get('cost') if isinstance(payload, dict) else None)
        try:
            cost = float(raw_cost)
        except (TypeError, ValueError):
            cost = 0.0
        if cost:
            self.last_cost_usd = cost
            if cost > self.config.max_turn_cost_usd:
                raise AppError(402, 'provider_cost',
                               f'LLM turn cost ${cost:.4f} exceeds the configured per-turn ceiling.')

        normalized = []
        for call in message.get('tool_calls') or []:
            try:
                function = call['function']
                arguments = json.loads(function.get('arguments') or '{}')
                if not isinstance(arguments, dict):
                    arguments = {}
                normalized.append({
                    'id': call.get('id') or 'tool_call',
                    'name': function['name'],
                    'arguments': arguments,
                    'raw_arguments': function.get('arguments') or '{}',
                })
            except (KeyError, TypeError, ValueError):
                continue
        assistant_message = {
            'role': 'assistant',
            'content': self._content(message.get('content')).strip() or None,
        }
        if normalized:
            assistant_message['tool_calls'] = [{
                'id': call['id'], 'type': 'function',
                'function': {'name': call['name'], 'arguments': call['raw_arguments']},
            } for call in normalized]
        return {'text': self._content(message.get('content')).strip(),
                'tool_calls': normalized, 'assistant_message': assistant_message}


def build_stt(config, transport=None):
    if config.pipeline_stt_provider == 'groq':
        return GroqSTT(config, transport)
    if config.pipeline_stt_provider == 'sarvam':
        return SarvamSTT(config, transport)
    if config.pipeline_stt_provider == 'openai':
        return OpenAISpeechComponents(config, transport)
    raise AppError(503, 'provider_not_configured', 'Unsupported pipeline STT provider.')


def build_tts(config, transport=None):
    if config.pipeline_tts_provider == 'sarvam':
        return SarvamTTS(config, transport)
    if config.pipeline_tts_provider == 'openai':
        return OpenAISpeechComponents(config, transport)
    raise AppError(503, 'provider_not_configured', 'Unsupported pipeline TTS provider.')


def build_llm(config, transport=None):
    return ToolCallingLLM(config, transport)
