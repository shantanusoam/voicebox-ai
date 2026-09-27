"""Authenticated lab WebSocket gateway, NOT an implemented Bluetooth driver."""
import asyncio
import base64
import contextlib
import json
import logging
import hashlib
import re
import time
from fastapi import WebSocket, WebSocketDisconnect
from .audio import (AudioBuffer, FRAME_BYTES, SAMPLE_RATE, to_wav, resample_async,
    unpack_audio_batch, pack_audio_batch, pack_adpcm_batch,
    DOWNLINK_BATCH_FRAMES, DOWNLINK_BATCH_MAGIC)
from .adpcm import encode_frame as encode_adpcm_frame
from .errors import AppError


AUTH_RECHECK_SECONDS = 5
TOUCH_FLUSH_SECONDS = 1
LOG = logging.getLogger(__name__)


async def send_paced_downlink(ws, output, epoch, current_epoch, codec='binary', lead_batches=2):
    """Prime 12 frames, then send no faster than the 20 ms playback clock.

    The ESP32 has one 20-frame receive/playback queue. A large initial burst
    overran its former 8-frame ingress queue, while 0.92x real-time pacing
    eventually filled even the larger queue. Slow writes may cause a playout
    gap; never catch up by bursting stale audio into a bounded device queue.
    """
    step = DOWNLINK_BATCH_FRAMES
    adpcm_state = (0, 0)
    for index in range(0, len(output), step * FRAME_BYTES):
        if index >= lead_batches * step * FRAME_BYTES:
            await asyncio.sleep(step * 0.02)
        if current_epoch() != epoch:
            return
        chunk = [output[start:start+FRAME_BYTES].ljust(FRAME_BYTES, b'\0')
                 for start in range(index, min(index + step * FRAME_BYTES, len(output)), FRAME_BYTES)]
        if codec == 'adpcm':
            blocks = []
            for frame in chunk:
                block, adpcm_state = encode_adpcm_frame(frame, adpcm_state)
                blocks.append(block)
            payload = pack_adpcm_batch(blocks, index // FRAME_BYTES, epoch)
        else:
            payload = pack_audio_batch(chunk, index // FRAME_BYTES, epoch, DOWNLINK_BATCH_MAGIC)
        await ws.send_bytes(payload)


async def send_binary_echo(ws, echoed, epoch, codec):
    for i in range(0, len(echoed), DOWNLINK_BATCH_FRAMES):
        group = echoed[i:i+DOWNLINK_BATCH_FRAMES]
        frames = [pcm for _, pcm in group]
        if codec == 'adpcm':
            state = (0, 0)
            blocks = []
            for frame in frames:
                block, state = encode_adpcm_frame(frame, state)
                blocks.append(block)
            payload = pack_adpcm_batch(blocks, group[0][0], epoch)
        else:
            payload = pack_audio_batch(frames, group[0][0], epoch, DOWNLINK_BATCH_MAGIC)
        await ws.send_bytes(payload)


_SENTENCE_BOUNDARY = re.compile(r'(?<=[.!?])\s+')

def split_reply_sentences(text):
    """First sentence alone (fast first audio); later fragments grouped so the
    TTS call count stays proportional to reply length, not punctuation."""
    parts = [p.strip() for p in _SENTENCE_BOUNDARY.split((text or '').strip()) if p.strip()]
    # Cap the first TTS fragment near 45 characters: TTS latency scales with
    # clip length, and the first fragment gates the first word. Cut at the
    # last comma inside the cap, else the last space.
    if parts and len(parts[0]) > 50:
        head = parts[0]
        cut = head.rfind(',', 0, 50)
        if cut < 20:
            cut = head.rfind(' ', 0, 50)
        if cut >= 20:
            parts[0] = head[:cut].strip(' ,')
            parts.insert(1, head[cut:].strip(' ,'))
    if len(parts) <= 1:
        return parts
    rest, group = [], []
    for p in parts[1:]:
        group.append(p)
        if len(' '.join(group)) >= 60:
            rest.append(' '.join(group)); group = []
    if group:
        rest.append(' '.join(group))
    return [parts[0]] + rest

class Gateway:
    def __init__(self, db, agent, provider, config):
        self.db, self.agent, self.provider, self.config = db, agent, provider, config
        self.connections = {}

    async def receive(self, ws, timeout=45):
        event = await asyncio.wait_for(ws.receive(), timeout)
        if event.get('type') == 'websocket.disconnect':
            raise WebSocketDisconnect(event.get('code', 1000))
        raw = event.get('text')
        binary = event.get('bytes')
        if binary is not None:
            # Binary is reserved for versioned batched PCM uplink. Keeping
            # control messages as JSON makes the protocol debuggable while
            # removing base64 + one-write-per-frame overhead from audio.
            if len(binary) > 6000:
                raise AppError(413, 'message_size', 'Gateway binary message exceeds 6000 bytes.')
            return unpack_audio_batch(binary)
        if raw is None:
            raise AppError(422, 'invalid_message', 'Expected a text or binary WebSocket message.')
        if len(raw) > 5000: raise AppError(413, 'message_size', 'Gateway message exceeds 5000 characters.')
        try: data = json.loads(raw)
        except (ValueError, TypeError): raise AppError(422, 'invalid_json', 'Expected a JSON object.')
        if not isinstance(data, dict): raise AppError(422, 'invalid_json', 'Expected a JSON object.')
        return data

    async def revoke(self, did):
        socket = self.connections.get(did)
        if socket:
            with contextlib.suppress(Exception): await socket.close(1008, 'Device revoked')

    async def handle(self, ws: WebSocket):
        await ws.accept()
        did = workspace = cid = None
        own_connection = False
        pending_frames = 0
        generation_task = None
        buffer = AudioBuffer()
        mode = 'echo'
        send_lock = asyncio.Lock()
        async def send(payload):
            async with send_lock: await ws.send_json(payload)
        async def fail(error):
            await send({'type':'error','code':error.code,'message':error.message})
        async def cancel_generation():
            nonlocal generation_task
            if generation_task and not generation_task.done():
                generation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception): await generation_task
            generation_task = None
        async def process_turn(pcm, call_id, epoch, request_id):
            try:
                payload=hashlib.sha256(pcm).hexdigest()
                cached=self.db.cached(workspace,'ws-audio:'+call_id,request_id,payload)
                if cached:
                    await send({'type':'turn.result','call_id':call_id,'epoch':epoch,**cached,'replayed':True})
                    await send({'type':'turn.done','call_id':call_id,'epoch':epoch})
                    return
                _t0 = time.monotonic()
                text = await self.provider.transcribe(to_wav(pcm), 'audio/wav')
                _t1 = time.monotonic()
                LOG.warning('turn-timing stt_ms=%d', int((_t1-_t0)*1000))
                result = await self.agent.turn(workspace, call_id, text, request_id)
                _t2 = time.monotonic()
                LOG.warning('turn-timing llm_ms=%d', int((_t2-_t1)*1000))
                self.db.cache(workspace,'ws-audio:'+call_id,request_id,payload,result)
                if buffer.epoch != epoch: return
                await send({'type':'turn.result','call_id':call_id,'epoch':epoch, **result})
                # Stream the reply sentence by sentence: first audio goes out
                # after one short TTS call; later sentences generate while the
                # device plays the previous ones. Same wire format, pacing
                # continues across sentences (prime only the very first one).
                sentences = split_reply_sentences(result['reply'])
                if not sentences:
                    sentences = [result['reply'] or '']
                for sentence_index, sentence in enumerate(sentences):
                    if buffer.epoch != epoch: return
                    _ts = time.monotonic()
                    wav = await self.provider.speak(sentence, wav=True)
                    output = await resample_async(wav)
                    LOG.warning('turn-timing tts_ms=%d sentence=%d len=%d since_start_ms=%d',
                             int((time.monotonic()-_ts)*1000), sentence_index,
                             len(output)//FRAME_BYTES*20, int((_ts-_t0)*1000))
                    if downlink_codec:
                        await send_paced_downlink(ws, output, epoch, lambda: buffer.epoch,
                                                  downlink_codec,
                                                  lead_batches=2 if sentence_index == 0 else 0)
                    else:
                        for index, start in enumerate(range(0, len(output), FRAME_BYTES)):
                            if buffer.epoch != epoch: return
                            frame = output[start:start+FRAME_BYTES].ljust(FRAME_BYTES, b'\0')
                            await send({'type':'audio.output','call_id':call_id,'epoch':epoch,'seq':index,
                                        'sample_rate':SAMPLE_RATE,'pcm16':base64.b64encode(frame).decode()})
                            await asyncio.sleep(.02)
                await send({'type':'turn.done','call_id':call_id,'epoch':epoch})
            except AppError as error:
                await fail(error)
                await send({'type':'turn.done','call_id':call_id,'epoch':epoch})
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Voice turn failed", extra={"call_id": call_id})
                await fail(AppError(500,'gateway_turn_error','Voice turn failed. Check stored call state before retrying.'))
                await send({'type':'turn.done','call_id':call_id,'epoch':epoch})
        try:
            hello = await self.receive(ws, 5)
            if hello.get('type') != 'hello' or hello.get('protocol') != 'callbox.v1':
                raise AppError(401, 'handshake', 'First message must be a callbox.v1 hello.')
            device = self.db.authenticate_device(hello.get('token'), hello.get('device_id'))
            if not device: raise AppError(401, 'device_auth', 'Invalid or revoked device credentials.')
            did, workspace = device['id'], device['workspace']
            # Same-token takeover. A device that reboots (or loses Wi-Fi) leaves
            # a socket here that the server has not yet noticed is dead, and the
            # device's own reconnect then gets rejected as device_busy until that
            # stale entry times out — observed on hardware as a device stuck
            # unauthenticated across a whole call. Credentials are per-device, so
            # a successful hello for a device that already has an entry means the
            # same physical device came back: close the old socket and let the new
            # one win rather than locking the real device out.
            stale = self.connections.pop(did, None)
            if stale is not None:
                self.db.event(workspace, 'device.reconnected',
                              'Replaced a stale gateway connection')
                with contextlib.suppress(Exception):
                    await stale.close(1012, 'Replaced by a newer connection')
            self.connections[did] = ws; own_connection = True
            # Negotiated CBB1 PCM or CBB2 IMA ADPCM; unrecognised/old devices
            # retain JSON audio.output. The codec is a per-connection choice.
            downlink_codec = hello.get('downlink') if hello.get('downlink') in {'binary', 'adpcm'} else None
            self.db.touch_device(did)
            self.db.event(workspace, 'device.connected', 'Lab gateway connected')
            await send({'type':'ready','protocol':'callbox.v1','device_id':did,'sample_rate':SAMPLE_RATE,
                        'frame_bytes':FRAME_BYTES,'max_turn_ms':15000,'hardware_verified':False})
            connected_at = time.monotonic()
            window, count = time.monotonic(), 0
            # Re-authenticating and touching the device on every message meant
            # two SQLite statements per 20 ms frame (~100/s/device) on the lock
            # shared with the HTTP API. Revocation still disconnects
            # immediately via revoke(); this bounds the out-of-band window.
            checked_at = time.monotonic()
            flushed_at = time.monotonic()
            while True:
                message = await self.receive(ws)
                now = time.monotonic()
                if now - window >= 1: window, count = now, 0
                count += 1
                if count > 150: raise AppError(429, 'message_rate', 'Gateway message rate exceeded.')
                if now - connected_at > self.config.max_call_seconds:
                    raise AppError(409, 'gateway_expired', 'Lab connection duration limit reached.')
                if now - checked_at >= AUTH_RECHECK_SECONDS:
                    checked_at = now
                    if not self.db.authenticate_device(hello.get('token'), did):
                        raise AppError(401, 'device_auth', 'Device credentials were revoked.')
                if now - flushed_at >= TOUCH_FLUSH_SECONDS:
                    flushed_at = now
                    self.db.touch_device(did, frames=pending_frames); pending_frames = 0
                kind = message.get('type')
                try:
                    if kind == 'ping':
                        await send({'type':'pong','time':time.time()}); continue
                    if kind == 'call.start':
                        if cid: raise AppError(409, 'call_active', 'End the current call first.')
                        mode = message.get('mode', 'echo')
                        if mode not in {'echo','agent'}: raise AppError(422, 'mode', 'Use echo or agent mode.')
                        if message.get('consent') is not True: raise AppError(422,'consent_required','Use consented test calls only.')
                        call = self.agent.start(workspace, label=device['name'] + ' - lab', source='device-lab',
                                                provider='openai' if mode == 'agent' else 'local', consent=True, device_id=did)
                        cid = call['id']; buffer = AudioBuffer()
                        await send({'type':'call.started','call_id':cid,'mode':mode,'epoch':buffer.epoch,'greeting':call['messages'][0]['text']})
                        continue
                    if not cid: raise AppError(409, 'no_call', 'Start a call first.')
                    # Binary audio batches are connection-scoped and omit the
                    # redundant call_id to keep the hardware wire header tiny.
                    if kind != 'audio.batch' and message.get('call_id') != cid:
                        raise AppError(409, 'wrong_call', 'Message is not for the active call.')
                    if kind == 'audio':
                        pcm = buffer.add(message)
                        pending_frames += 1
                        if mode == 'echo':
                            buffer.take()
                            if downlink_codec:
                                await send_binary_echo(ws, [(message['seq'], pcm)],
                                                       buffer.epoch, downlink_codec)
                            else:
                                await send({'type':'audio.output','call_id':cid,'epoch':buffer.epoch,
                                            'seq':message['seq'],'sample_rate':SAMPLE_RATE,
                                            'pcm16':base64.b64encode(pcm).decode()})
                    elif kind == 'audio.batch':
                        frames = message['frames']
                        first_seq = message['first_seq']
                        epoch = message['epoch']
                        echoed = []
                        for index, pcm in enumerate(frames):
                            seq = first_seq + index
                            echoed.append((seq, buffer.add_pcm(pcm, seq, epoch)))
                            pending_frames += 1
                        if mode == 'echo':
                            buffer.take()
                            if downlink_codec:
                                await send_binary_echo(ws, echoed, buffer.epoch, downlink_codec)
                            else:
                                for seq, pcm in echoed:
                                    await send({'type':'audio.output','call_id':cid,'epoch':buffer.epoch,
                                                'seq':seq,'sample_rate':SAMPLE_RATE,
                                                'pcm16':base64.b64encode(pcm).decode()})
                    elif kind == 'audio.commit':
                        if mode != 'agent': raise AppError(409, 'mode', 'Echo mode does not send audio to a model.')
                        if generation_task and not generation_task.done(): raise AppError(409,'busy','Interrupt or wait for the current turn.')
                        request_id = message.get('request_id')
                        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request_id):
                            raise AppError(422,'request_id','A unique request ID is required.')
                        pcm = buffer.take()
                        if len(pcm) < 3200: raise AppError(422,'audio_short','Capture at least 100 ms of audio.')
                        generation_task = asyncio.create_task(process_turn(pcm, cid, buffer.epoch, request_id))
                        await send({'type':'turn.processing','call_id':cid,'epoch':buffer.epoch})
                    elif kind == 'call.text':
                        if generation_task and not generation_task.done(): raise AppError(409,'busy','Voice turn is still running.')
                        text = message.get('text', '')
                        result = await self.agent.turn(workspace, cid, text, message.get('request_id', ''))
                        await send({'type':'turn.result','call_id':cid, **result})
                    elif kind == 'interrupt':
                        epoch = buffer.interrupt()
                        await cancel_generation()
                        await send({'type':'playback.clear','call_id':cid,'epoch':epoch,
                                    'note':'Drop queued playback. Already committed bookings are not undone.'})
                    elif kind == 'call.end':
                        await cancel_generation()
                        self.db.end_call(workspace, cid)
                        self.agent.locks.pop(cid, None)
                        await send({'type':'call.ended','call_id':cid})
                        cid = None
                    else: raise AppError(422, 'message_type', 'Unknown gateway message type.')
                except AppError as error:
                    await fail(error)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except AppError as error:
            with contextlib.suppress(Exception):
                await fail(error); await ws.close(1008)
        except Exception:
            LOG.exception("Unexpected device gateway failure", extra={"device_id": did, "call_id": cid})
            with contextlib.suppress(Exception): await ws.close(1011, 'Gateway failure')
        finally:
            await cancel_generation()
            if cid:
                self.db.end_call(workspace, cid, 'Gateway disconnected')
                self.agent.locks.pop(cid, None)
            # Only tear down device state if this socket is still the registered
            # one. A newer connection may have taken over (see the takeover above),
            # and this handler unwinding afterwards must not evict the live socket
            # or mark a connected device offline.
            if did and own_connection and self.connections.get(did) is ws:
                self.connections.pop(did, None)
                # Flush the batched frame count before going offline.
                self.db.touch_device(did, online=False, frames=pending_frames)
                self.db.event(workspace, 'device.disconnected', 'Lab gateway disconnected')
