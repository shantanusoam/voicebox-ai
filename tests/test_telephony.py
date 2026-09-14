"""Telephony transport: G.711 codecs, rate conversion and AudioSocket framing."""
import base64
import json
import math
import struct

import pytest

from callbox import telephony as t


def tone(samples=800, rate=8000, amplitude=8000, hz=440):
    return b''.join(struct.pack('<h', int(amplitude * math.sin(2 * math.pi * hz * i / rate)))
                    for i in range(samples))


# --- codecs -------------------------------------------------------------

def test_ulaw_round_trip_stays_within_companding_error():
    pcm = tone()
    encoded = t.pcm16_to_ulaw(pcm)
    assert len(encoded) == len(pcm) // 2, 'G.711 is one byte per sample'
    decoded = t.ulaw_to_pcm16(encoded)
    assert len(decoded) == len(pcm)
    worst = max(abs(struct.unpack('<h', pcm[i*2:i*2+2])[0] -
                    struct.unpack('<h', decoded[i*2:i*2+2])[0]) for i in range(len(pcm)//2))
    # 8-bit companding is lossy by design; this bounds it well under 1% FS.
    assert worst < 330, f'mu-law error {worst} is larger than companding explains'


def test_ulaw_handles_the_full_sample_range():
    extremes = struct.pack('<4h', -32768, 32767, 0, -1)
    assert len(t.ulaw_to_pcm16(t.pcm16_to_ulaw(extremes))) == len(extremes)


def test_alaw_decodes_every_byte():
    decoded = t.alaw_to_pcm16(bytes(range(256)))
    assert len(decoded) == 512


def test_codecs_tolerate_an_odd_trailing_byte():
    assert t.pcm16_to_ulaw(b'\x01\x02\x03') == t.pcm16_to_ulaw(b'\x01\x02')


# --- rate conversion ----------------------------------------------------

def test_rate_conversion_uses_the_exact_three_to_one_ratio():
    """The Realtime API refuses input below 24 kHz, so 8 kHz must be raised."""
    assert t.REALTIME_RATE // t.TELEPHONY_RATE == 3
    wide = t.upsample_to_realtime(tone())
    assert len(wide) == len(tone()) * 3
    narrow = t.downsample_from_realtime(wide)
    assert len(narrow) == len(tone())


def test_upsampling_preserves_the_signal_shape():
    pcm = tone()
    narrow = t.downsample_from_realtime(t.upsample_to_realtime(pcm))
    worst = max(abs(struct.unpack('<h', pcm[i*2:i*2+2])[0] -
                    struct.unpack('<h', narrow[i*2:i*2+2])[0]) for i in range(len(pcm)//2))
    assert worst < 1200, f'round trip distorted the waveform by {worst}'


def test_rate_conversion_handles_empty_and_short_input():
    assert t.upsample_to_realtime(b'') == b''
    assert t.downsample_from_realtime(b'') == b''
    assert t.downsample_from_realtime(b'\x00\x00') == b''      # less than one group


# --- AudioSocket framing ------------------------------------------------

def test_frame_round_trip():
    decoder = t.AudioSocketDecoder()
    blob = t.encode_frame(t.KIND_UUID, bytes(16)) + t.encode_frame(t.KIND_AUDIO, bytes(320))
    assert decoder.feed(blob) == [(t.KIND_UUID, bytes(16)), (t.KIND_AUDIO, bytes(320))]


def test_frames_split_across_tcp_reads_are_reassembled():
    """TCP delivers arbitrary chunks, never whole frames."""
    decoder = t.AudioSocketDecoder()
    blob = t.encode_frame(t.KIND_AUDIO, tone(160))
    assert decoder.feed(blob[:5]) == []
    assert decoder.feed(blob[5:17]) == []
    frames = decoder.feed(blob[17:])
    assert len(frames) == 1 and frames[0][0] == t.KIND_AUDIO


def test_multiple_frames_in_one_read():
    decoder = t.AudioSocketDecoder()
    blob = b''.join(t.encode_frame(t.KIND_AUDIO, bytes(320)) for _ in range(5))
    assert len(decoder.feed(blob)) == 5


def test_desynchronised_stream_is_rejected_not_buffered_forever():
    decoder = t.AudioSocketDecoder(limit=64)
    with pytest.raises(ValueError):
        for _ in range(50):
            decoder.feed(b'\x10\xff\xff' + bytes(100))


def test_oversized_payload_refused():
    with pytest.raises(ValueError):
        t.encode_frame(t.KIND_AUDIO, bytes(70000))


# --- the call leg -------------------------------------------------------

class FakeWriter:
    def __init__(self): self.sent = bytearray()
    def write(self, data): self.sent.extend(data)
    async def drain(self): pass
    def close(self): pass
    def get_extra_info(self, _): return ('127.0.0.1', 1234)


class FakeReader:
    def __init__(self, chunks): self.chunks = list(chunks)
    async def read(self, _): return self.chunks.pop(0) if self.chunks else b''


def test_leg_converts_inbound_telephony_audio_to_realtime_rate():
    import asyncio
    leg = t.AudioSocketLeg(FakeReader([t.encode_frame(t.KIND_AUDIO, tone(160))]), FakeWriter())
    message = json.loads(asyncio.run(leg.receive_text()))
    assert message['type'] == 'audio'
    assert len(base64.b64decode(message['pcm16'])) == 320 * 3, 'must be upsampled to 24 kHz'


def test_leg_reports_hangup_as_end():
    import asyncio
    leg = t.AudioSocketLeg(FakeReader([b'']), FakeWriter())
    assert json.loads(asyncio.run(leg.receive_text()))['type'] == 'end'


def test_leg_drops_queued_audio_on_barge_in():
    import asyncio

    async def run():
        leg = t.AudioSocketLeg(FakeReader([]), FakeWriter())
        await leg.send_json({'type': 'audio', 'pcm16': base64.b64encode(tone(2400, 24000)).decode()})
        assert leg.outbound, 'audio should be queued for the phone'
        await leg.send_json({'type': 'playback.clear'})
        assert not leg.outbound, 'barge-in must drop audio the caller has not heard'
        leg.closed = True
    asyncio.run(run())


def test_leg_captures_dtmf():
    import asyncio
    digits = []
    leg = t.AudioSocketLeg(FakeReader([t.encode_frame(t.KIND_DTMF, b'7'),
                                       t.encode_frame(t.KIND_AUDIO, tone(160))]),
                           FakeWriter(), on_dtmf=digits.append)
    asyncio.run(leg.receive_text())
    assert digits == ['7'], 'DTMF is the reliable fallback for digits on a narrowband line'
