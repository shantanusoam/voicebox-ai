import ctypes
import math
from pathlib import Path
import struct
import subprocess

from callbox.adpcm import BLOCK_BYTES, decode_frame, encode_frame
from callbox.audio import ADPCM_BATCH_MAGIC, DOWNLINK_BATCH_HEADER, FRAME_BYTES


def wave_frame(offset):
    return b''.join(struct.pack('<h', int(9000 * math.sin(2 * math.pi * 440 * (offset + n) / 16000)))
                    for n in range(320))


def test_adpcm_block_matches_device_decoder(tmp_path):
    root = Path(__file__).resolve().parents[1]
    main = root / 'firmware/esp32-hfp-bridge/callbox_hfp/main'
    shared = tmp_path / 'libcallbox_adpcm.so'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                    str(main / 'adpcm.c'), '-o', str(shared)], check=True)
    decoder = ctypes.CDLL(str(shared)).cb_adpcm_decode
    decoder.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    decoder.restype = ctypes.c_bool
    state = (0, 0)
    for offset in (0, 320, 640, 960):
        source = wave_frame(offset)
        block, state = encode_frame(source, state)
        assert len(block) == BLOCK_BYTES
        out = ctypes.create_string_buffer(FRAME_BYTES)
        assert decoder(block, len(block), out, FRAME_BYTES)
        assert out.raw == decode_frame(block)
        actual = struct.unpack('<320h', out.raw)
        expected = struct.unpack('<320h', source)
        assert sum(abs(a - b) for a, b in zip(actual, expected)) / 320 < 1000


def test_adpcm_independent_frame_and_invalid_header(tmp_path):
    source = wave_frame(0)
    block, state = encode_frame(source)
    assert len(decode_frame(block)) == FRAME_BYTES
    # A later block can be decoded even when the first is lost.
    next_block, _ = encode_frame(wave_frame(320), state)
    assert len(decode_frame(next_block)) == FRAME_BYTES
    bad = bytearray(next_block)
    bad[2] = 89
    try:
        decode_frame(bad)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid step index accepted')
