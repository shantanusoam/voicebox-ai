"""Telephony transport: G.711 narrowband <-> the 24 kHz realtime contract.

A SIP call carries 8 kHz G.711 (mu-law in India/US, A-law in most of EU), and
the Realtime API refuses anything below 24 kHz. This module is the adapter
between the two, plus the Asterisk AudioSocket framing that carries call audio
to us over TCP.

Upsampling 8 kHz to 24 kHz satisfies the API; it does not restore information
the narrowband channel never carried. Expect worse recognition on names and
digits than the browser path, and confirm both by readback or DTMF.

audioop was removed in Python 3.13, so the codecs are implemented here.
"""
from array import array
import struct
import sys

TELEPHONY_RATE = 8000
REALTIME_RATE = 24000
RATIO = REALTIME_RATE // TELEPHONY_RATE      # exactly 3
FRAME_MS = 20
TELEPHONY_FRAME_SAMPLES = TELEPHONY_RATE * FRAME_MS // 1000    # 160
TELEPHONY_FRAME_BYTES = TELEPHONY_FRAME_SAMPLES * 2            # 320 for L16

BIAS = 0x84
CLIP = 32635


def _ulaw_encode_sample(sample):
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_ULAW_DECODE = []
for _byte in range(256):
    _value = ~_byte & 0xFF
    _sign, _exponent, _mantissa = _value & 0x80, (_value >> 4) & 0x07, _value & 0x0F
    _sample = ((_mantissa << 3) + BIAS) << _exponent
    _sample -= BIAS
    _ULAW_DECODE.append(-_sample if _sign else _sample)

_ULAW_ENCODE = bytes(_ulaw_encode_sample(s if s < 32768 else s - 65536) for s in range(65536))[:0] or None


def ulaw_to_pcm16(payload: bytes) -> bytes:
    out = array('h', [_ULAW_DECODE[b] for b in payload])
    if sys.byteorder != 'little':
        out.byteswap()
    return out.tobytes()


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    samples = array('h')
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != 'little':
        samples.byteswap()
    return bytes(_ulaw_encode_sample(s) for s in samples)


_ALAW_DECODE = []
for _byte in range(256):
    _value = _byte ^ 0x55
    _sign = _value & 0x80
    _exponent = (_value & 0x70) >> 4
    _mantissa = _value & 0x0F
    _sample = (_mantissa << 4) + 8
    if _exponent:
        _sample = (_sample + 0x100) << (_exponent - 1)
    _ALAW_DECODE.append(-_sample if _sign else _sample)


def alaw_to_pcm16(payload: bytes) -> bytes:
    out = array('h', [_ALAW_DECODE[b] for b in payload])
    if sys.byteorder != 'little':
        out.byteswap()
    return out.tobytes()


def upsample_to_realtime(pcm8k: bytes) -> bytes:
    """8 kHz -> 24 kHz by linear interpolation across an exact 1:3 ratio."""
    source = array('h')
    source.frombytes(pcm8k[:len(pcm8k) - len(pcm8k) % 2])
    if sys.byteorder != 'little':
        source.byteswap()
    if not source:
        return b''
    out = array('h', bytes(2 * len(source) * RATIO))
    last = len(source) - 1
    index = 0
    for i, value in enumerate(source):
        following = source[i + 1] if i < last else value
        step = (following - value) / RATIO
        for k in range(RATIO):
            out[index] = max(-32768, min(32767, int(value + step * k)))
            index += 1
    if sys.byteorder != 'little':
        out.byteswap()
    return out.tobytes()


def downsample_from_realtime(pcm24k: bytes) -> bytes:
    """24 kHz -> 8 kHz, averaging each group of three to limit aliasing."""
    source = array('h')
    source.frombytes(pcm24k[:len(pcm24k) - len(pcm24k) % 2])
    if sys.byteorder != 'little':
        source.byteswap()
    usable = len(source) - len(source) % RATIO
    out = array('h', [sum(source[i:i + RATIO]) // RATIO for i in range(0, usable, RATIO)])
    if sys.byteorder != 'little':
        out.byteswap()
    return out.tobytes()


# --- Asterisk AudioSocket ------------------------------------------------
# Frame: 1 byte type, 2 bytes big-endian length, then payload.
KIND_TERMINATE = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF


def encode_frame(kind: int, payload: bytes = b'') -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError('AudioSocket payload exceeds one frame')
    return struct.pack('>BH', kind, len(payload)) + payload


class AudioSocketDecoder:
    """Incremental parser: TCP gives arbitrary chunks, not whole frames."""

    def __init__(self, limit=65535):
        self.buffer = bytearray()
        self.limit = limit

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        if len(self.buffer) > self.limit * 4:
            raise ValueError('AudioSocket stream desynchronised')
        frames = []
        while len(self.buffer) >= 3:
            kind, length = struct.unpack('>BH', self.buffer[:3])
            if len(self.buffer) < 3 + length:
                break
            payload = bytes(self.buffer[3:3 + length])
            del self.buffer[:3 + length]
            frames.append((kind, payload))
        return frames


# --- Bridging a call leg into the realtime engine ------------------------
import asyncio
import base64
import json


class AudioSocketLeg:
    """Presents a SIP call leg with the same surface as the browser socket.

    realtime.run() is written against `receive_text()` / `send_json()`, so a
    phone call can reuse the identical engine, tools and guarantees as the
    console. Audio is translated at this boundary and nowhere else:
    AudioSocket carries 8 kHz signed-linear mono, the Realtime API wants
    24 kHz.
    """

    def __init__(self, reader, writer, on_dtmf=None):
        self.reader, self.writer = reader, writer
        self.decoder = AudioSocketDecoder()
        self.pending = asyncio.Queue()
        self.call_uuid = None
        self.on_dtmf = on_dtmf
        self.closed = False
        self.outbound = bytearray()      # 24 kHz from the model, awaiting paced send
        self.sender = None

    # -- inbound (caller -> model) ------------------------------------
    async def receive_text(self):
        while True:
            if not self.pending.empty():
                return await self.pending.get()
            chunk = await self.reader.read(4096)
            if not chunk:
                self.closed = True
                return json.dumps({'type': 'end'})
            for kind, payload in self.decoder.feed(chunk):
                if kind == KIND_AUDIO and payload:
                    wide = upsample_to_realtime(payload)
                    await self.pending.put(json.dumps(
                        {'type': 'audio', 'pcm16': base64.b64encode(wide).decode()}))
                elif kind == KIND_UUID:
                    self.call_uuid = payload.hex()
                elif kind == KIND_DTMF:
                    digit = payload.decode('ascii', 'ignore')
                    if self.on_dtmf and digit:
                        self.on_dtmf(digit)
                elif kind in (KIND_TERMINATE, KIND_ERROR):
                    self.closed = True
                    await self.pending.put(json.dumps({'type': 'end'}))

    # -- outbound (model -> caller) -----------------------------------
    async def send_json(self, payload):
        kind = payload.get('type')
        if kind == 'audio':
            data = payload.get('pcm16')
            if data:
                self.outbound.extend(downsample_from_realtime(base64.b64decode(data)))
                self.ensure_sender()
        elif kind == 'playback.clear':
            # Barge-in: drop everything not yet handed to the phone.
            self.outbound.clear()

    def ensure_sender(self):
        if self.sender is None or self.sender.done():
            self.sender = asyncio.create_task(self.drain())

    async def drain(self):
        """Hand the phone exactly one 20 ms frame every 20 ms.

        A phone call has no jitter buffer on our side: writing faster than
        real time makes the caller hear chipmunk audio or overruns Asterisk.
        """
        try:
            while not self.closed:
                if len(self.outbound) < TELEPHONY_FRAME_BYTES:
                    if not self.outbound:
                        return
                    self.outbound.extend(bytes(TELEPHONY_FRAME_BYTES - len(self.outbound)))
                frame = bytes(self.outbound[:TELEPHONY_FRAME_BYTES])
                del self.outbound[:TELEPHONY_FRAME_BYTES]
                self.writer.write(encode_frame(KIND_AUDIO, frame))
                await self.writer.drain()
                await asyncio.sleep(FRAME_MS / 1000)
        except (ConnectionResetError, BrokenPipeError):
            self.closed = True

    async def hangup(self):
        self.closed = True
        with_suppress = (ConnectionResetError, BrokenPipeError, RuntimeError)
        try:
            self.writer.write(encode_frame(KIND_TERMINATE))
            await self.writer.drain()
        except with_suppress:
            pass
        try:
            self.writer.close()
        except with_suppress:
            pass
