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
from .audio import AudioBuffer, FRAME_BYTES, SAMPLE_RATE, to_wav, resample_async, unpack_audio_batch
from .errors import AppError


AUTH_RECHECK_SECONDS = 5
TOUCH_FLUSH_SECONDS = 1
LOG = logging.getLogger(__name__)


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
                text = await self.provider.transcribe(to_wav(pcm), 'audio/wav')
                result = await self.agent.turn(workspace, call_id, text, request_id)
                self.db.cache(workspace,'ws-audio:'+call_id,request_id,payload,result)
                if buffer.epoch != epoch: return
                await send({'type':'turn.result','call_id':call_id,'epoch':epoch, **result})
                wav = await self.provider.speak(result['reply'], wav=True)
                output = await resample_async(wav)
                for index, start in enumerate(range(0, len(output), FRAME_BYTES)):
                    if buffer.epoch != epoch: return
                    frame = output[start:start+FRAME_BYTES].ljust(FRAME_BYTES, b'\0')
                    await send({'type':'audio.output','call_id':call_id,'epoch':epoch,'seq':index,
                                'sample_rate':SAMPLE_RATE,'pcm16':base64.b64encode(frame).decode()})
                    await asyncio.sleep(.02)
                await send({'type':'turn.done','call_id':call_id,'epoch':epoch})
            except AppError as error: await fail(error)
            except asyncio.CancelledError: raise
            except Exception:
                await fail(AppError(500,'gateway_turn_error','Voice turn failed. Check stored call state before retrying.'))
        try:
            hello = await self.receive(ws, 5)
            if hello.get('type') != 'hello' or hello.get('protocol') != 'callbox.v1':
                raise AppError(401, 'handshake', 'First message must be a callbox.v1 hello.')
            device = self.db.authenticate_device(hello.get('token'), hello.get('device_id'))
            if not device: raise AppError(401, 'device_auth', 'Invalid or revoked device credentials.')
            did, workspace = device['id'], device['workspace']
            if did in self.connections:
                raise AppError(409, 'device_busy', 'This device already has an active connection.')
            self.connections[did] = ws; own_connection = True
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
                            await send({'type':'audio.output','call_id':cid,'epoch':buffer.epoch,
                                        'seq':message['seq'],'sample_rate':SAMPLE_RATE,'pcm16':base64.b64encode(pcm).decode()})
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
                            # Echo each logical 20 ms frame so the existing
                            # device downlink remains protocol-compatible.
                            buffer.take()
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
            if did and own_connection:
                self.connections.pop(did, None)
                # Flush the batched frame count before going offline.
                self.db.touch_device(did, online=False, frames=pending_frames)
                self.db.event(workspace, 'device.disconnected', 'Lab gateway disconnected')
