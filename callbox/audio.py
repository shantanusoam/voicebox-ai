"""Fixed PCM16/mono/16 kHz device contract and bounded turn buffers."""
import asyncio
from array import array
import base64
import binascii
import io
import math
import sys
import struct
import wave
from .errors import AppError

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_TURN_BYTES = SAMPLE_RATE * 2 * 15
AUDIO_BATCH_MAGIC = b'CBA1'
AUDIO_BATCH_HEADER = struct.Struct('>4sBBHII')
MAX_AUDIO_BATCH_FRAMES = 8
MAX_FORWARD_SEQUENCE_GAP = 10

# Below this RMS (of a 32768 full-scale signal) a turn is treated as having no
# speech in it. Measured reference points: digital silence 0, a quiet room
# floor well under 100, generated speech in the low thousands. This gate is
# deliberately conservative and exists because a chat model asked to
# transcribe silence invents a plausible sentence instead of saying nothing.
SILENCE_RMS = 150


def decode_frame(message):
    if message.get('sample_rate') != SAMPLE_RATE or message.get('channels', 1) != 1:
        raise AppError(422, 'audio_format', 'Expected mono PCM16 little-endian at 16000 Hz.')
    encoded = message.get('pcm16', '')
    if not isinstance(encoded, str) or len(encoded) > 1000:
        raise AppError(422, 'frame_size', 'Expected one 20 ms PCM frame.')
    try: pcm = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError): raise AppError(422, 'invalid_audio', 'Invalid base64 PCM.')
    if len(pcm) != FRAME_BYTES: raise AppError(422, 'frame_size', 'Every frame must contain exactly 640 bytes (20 ms).')
    seq = message.get('seq')
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 2**31 - 1:
        raise AppError(422, 'sequence', 'Frame sequence must be a nonnegative integer.')
    return pcm, seq


def pack_audio_batch(frames, first_seq: int, epoch: int = 0) -> bytes:
    """Versioned binary audio batch used by constrained hardware transports.

    Header (network byte order): magic[4], frame_count u8, flags u8,
    frame_bytes u16, first_seq u32, epoch u32; followed by raw PCM frames.
    """
    frames = list(frames)
    if not 1 <= len(frames) <= MAX_AUDIO_BATCH_FRAMES:
        raise ValueError('audio batch must contain 1..8 frames')
    if not 0 <= first_seq <= 2**31 - 1 or not 0 <= epoch <= 2**32 - 1:
        raise ValueError('invalid audio batch sequence or epoch')
    if any(not isinstance(frame, (bytes, bytearray)) or len(frame) != FRAME_BYTES for frame in frames):
        raise ValueError('audio batch frames must be 640-byte PCM16 frames')
    header = AUDIO_BATCH_HEADER.pack(
        AUDIO_BATCH_MAGIC, len(frames), 0, FRAME_BYTES, first_seq, epoch
    )
    return header + b''.join(frames)


def unpack_audio_batch(payload: bytes):
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < AUDIO_BATCH_HEADER.size:
        raise AppError(422, 'audio_batch', 'Invalid binary audio batch.')
    magic, count, flags, frame_bytes, first_seq, epoch = AUDIO_BATCH_HEADER.unpack_from(payload)
    if magic != AUDIO_BATCH_MAGIC or flags != 0 or frame_bytes != FRAME_BYTES:
        raise AppError(422, 'audio_batch', 'Unsupported binary audio batch format.')
    if not 1 <= count <= MAX_AUDIO_BATCH_FRAMES or first_seq > 2**31 - 1:
        raise AppError(422, 'audio_batch', 'Invalid binary audio batch metadata.')
    expected = AUDIO_BATCH_HEADER.size + count * FRAME_BYTES
    if len(payload) != expected:
        raise AppError(422, 'audio_batch', 'Binary audio batch length does not match its header.')
    start = AUDIO_BATCH_HEADER.size
    frames = [bytes(payload[start+i*FRAME_BYTES:start+(i+1)*FRAME_BYTES]) for i in range(count)]
    return {'type':'audio.batch', 'first_seq':first_seq, 'epoch':epoch, 'frames':frames}


def pcm_rms(pcm: bytes) -> float:
    """Root-mean-square level of mono PCM16LE, 0 for an empty buffer."""
    samples = array('h')
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != 'little': samples.byteswap()
    if not samples: return 0.0
    return math.sqrt(sum(v * v for v in samples) / len(samples))


def wav_rms(wav_bytes: bytes):
    """RMS of a WAV payload, or None when it cannot be read as mono PCM16."""
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2: return None
            return pcm_rms(wav.readframes(wav.getnframes()))
    except (wave.Error, EOFError, ValueError):
        return None


def to_wav(pcm: bytes, sample_rate=SAMPLE_RATE):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(pcm)
    return output.getvalue()


def wav_to_pcm16(wav_bytes: bytes):
    """Decode a provider WAV to PCM16 at SAMPLE_RATE.

    CPU-bound and O(samples). Call it through resample_async() from async
    code: a 60 s 48 kHz reply is ~2.9 M samples, and running that inline
    stalled the event loop for every other call, the gateway heartbeat and
    the message-rate window.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise AppError(502, 'tts_format', 'Expected mono PCM16 WAV from speech provider.')
            rate = wav.getframerate()
            if not 8000 <= rate <= 48000 or wav.getnframes()/rate > 60:
                raise AppError(502, 'tts_format', 'Unsupported rate or oversized generated speech.')
            data = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError): raise AppError(502, 'tts_format', 'Provider response is not a PCM WAV file.')
    return resample_pcm16(data, rate)


def resample_pcm16(data: bytes, rate: int) -> bytes:
    """Resample mono PCM16LE to SAMPLE_RATE.

    Linear interpolation is adequate for this laboratory transport test.
    Replace with an evaluated resampler for production voice quality.
    """
    if rate == SAMPLE_RATE: return data
    source = array('h'); source.frombytes(data)
    if sys.byteorder != 'little': source.byteswap()
    if len(source) < 2: return b''
    if rate % SAMPLE_RATE == 0:
        # Integer decimation (48k/32k -> 16k). Box-average each group so the
        # discarded band is attenuated instead of aliased straight back in.
        step = rate // SAMPLE_RATE
        output = array('h', [sum(source[i:i+step]) // step
                             for i in range(0, len(source) - step + 1, step)])
    else:
        # Rational rates (24k -> 16k). Hoist lookups out of the loop.
        count = int(len(source) * SAMPLE_RATE / rate)
        ratio = rate / SAMPLE_RATE
        last = len(source) - 1
        output = array('h', [0]) * count
        for n in range(count):
            index = n * ratio
            left = int(index)
            if left >= last:
                output[n] = source[last]
                continue
            value = source[left] + (source[left+1] - source[left]) * (index - left)
            output[n] = max(-32768, min(32767, round(value)))
    if sys.byteorder != 'little': output.byteswap()
    return output.tobytes()


async def resample_async(wav_bytes: bytes) -> bytes:
    """wav_to_pcm16 on a worker thread, so the event loop keeps serving."""
    return await asyncio.to_thread(wav_to_pcm16, wav_bytes)


class AudioBuffer:
    def __init__(self):
        self.data = bytearray(); self.seq = -1; self.epoch = 0; self.lost_frames = 0
    def add_pcm(self, pcm: bytes, seq: int, epoch: int = 0):
        if epoch != self.epoch: raise AppError(409, 'stale_audio', 'Audio belongs to a cancelled playback epoch.')
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) != FRAME_BYTES:
            raise AppError(422, 'frame_size', 'Every frame must contain exactly 640 bytes (20 ms).')
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 2**31 - 1:
            raise AppError(422, 'sequence', 'Frame sequence must be a nonnegative integer.')
        expected = self.seq + 1
        if seq < expected:
            raise AppError(409, 'sequence', 'Duplicate or out-of-order audio frame.')
        gap = seq - expected
        # The very first frame still establishes a strict seq=0 contract. Once
        # a stream is established, bounded forward loss degrades to silence
        # instead of poisoning every later real-time frame.
        if self.seq < 0 and gap:
            raise AppError(409, 'sequence', 'First audio frame must use sequence 0.')
        if gap > MAX_FORWARD_SEQUENCE_GAP:
            raise AppError(409, 'sequence', 'Audio sequence gap exceeds the recovery window.')
        extra = gap * FRAME_BYTES
        if len(self.data) + extra + len(pcm) > MAX_TURN_BYTES:
            raise AppError(413, 'audio_buffer_full', 'Commit or interrupt before the 15-second turn limit.')
        if gap:
            self.data.extend(b'\0' * extra)
            self.lost_frames += gap
        self.seq = seq; self.data.extend(pcm)
        return bytes(pcm)
    def add(self, message):
        pcm, seq = decode_frame(message)
        return self.add_pcm(pcm, seq, message.get('epoch', 0))
    def take(self):
        pcm = bytes(self.data); self.data.clear()
        return pcm
    def interrupt(self):
        self.data.clear(); self.epoch += 1
        return self.epoch
