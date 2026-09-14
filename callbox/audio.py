"""Fixed PCM16/mono/16 kHz device contract and bounded turn buffers."""
from array import array
import base64
import binascii
import io
import math
import sys
import wave
from .errors import AppError

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_TURN_BYTES = SAMPLE_RATE * 2 * 15


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


def to_wav(pcm: bytes, sample_rate=SAMPLE_RATE):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(pcm)
    return output.getvalue()


def wav_to_pcm16(wav_bytes: bytes):
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise AppError(502, 'tts_format', 'Expected mono PCM16 WAV from speech provider.')
            rate = wav.getframerate()
            if not 8000 <= rate <= 48000 or wav.getnframes()/rate > 60:
                raise AppError(502, 'tts_format', 'Unsupported rate or oversized generated speech.')
            data = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError): raise AppError(502, 'tts_format', 'Provider response is not a PCM WAV file.')
    if rate == SAMPLE_RATE: return data
    # Linear interpolation is adequate for this laboratory transport test.
    # Replace with an evaluated resampler for production voice quality.
    source = array('h'); source.frombytes(data)
    if sys.byteorder != 'little': source.byteswap()
    if len(source) < 2: return b''
    output = array('h')
    for n in range(int(len(source)*SAMPLE_RATE/rate)):
        index = n * rate / SAMPLE_RATE
        left = min(int(index), len(source)-1); right = min(left+1, len(source)-1)
        value = round(source[left] + (source[right]-source[left])*(index-left))
        output.append(max(-32768, min(32767, value)))
    if sys.byteorder != 'little': output.byteswap()
    return output.tobytes()


class AudioBuffer:
    def __init__(self):
        self.data = bytearray(); self.seq = -1; self.epoch = 0
    def add(self, message):
        if message.get('epoch', 0) != self.epoch: raise AppError(409, 'stale_audio', 'Audio belongs to a cancelled playback epoch.')
        pcm, seq = decode_frame(message)
        if seq != self.seq + 1: raise AppError(409, 'sequence', 'Out-of-order or missing audio frame.')
        if len(self.data) + len(pcm) > MAX_TURN_BYTES:
            raise AppError(413, 'audio_buffer_full', 'Commit or interrupt before the 15-second turn limit.')
        self.seq = seq; self.data.extend(pcm)
        return pcm
    def take(self):
        pcm = bytes(self.data); self.data.clear()
        return pcm
    def interrupt(self):
        self.data.clear(); self.epoch += 1
        return self.epoch
