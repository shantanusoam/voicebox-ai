"""Small, independent 20 ms IMA ADPCM blocks for the ESP32 CBB2 downlink.

Each 164-byte block carries a signed predictor, step index, reserved byte and
320 four-bit codes (160 bytes). The state header lets a receiver decode any
frame after loss without relying on the previous packet.
"""
import struct
from array import array
import sys

PCM_BYTES = 640
BLOCK_BYTES = 164
STEPS = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
         34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
         143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408,
         449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
         1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749,
         3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484,
         7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
         15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767)
INDEX = (-1, -1, -1, -1, 2, 4, 6, 8)


def advance(predictor, index, code):
    step = STEPS[index]
    delta = step >> 3
    if code & 1: delta += step >> 2
    if code & 2: delta += step >> 1
    if code & 4: delta += step
    predictor += -delta if code & 8 else delta
    predictor = max(-32768, min(32767, predictor))
    index = max(0, min(88, index + INDEX[code & 7]))
    return predictor, index


def encode_frame(pcm: bytes, state=(0, 0)):
    if len(pcm) != PCM_BYTES: raise ValueError('expected 640 PCM bytes')
    predictor, index = state
    if not -32768 <= predictor <= 32767 or not 0 <= index <= 88:
        raise ValueError('invalid ADPCM state')
    samples = array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little': samples.byteswap()
    block = bytearray(struct.pack('<hBB', predictor, index, 0))
    packed = 0
    for n, sample in enumerate(samples):
        step = STEPS[index]
        diff = sample - predictor
        code = 8 if diff < 0 else 0
        diff = abs(diff)
        if diff >= step: code |= 4; diff -= step
        if diff >= step >> 1: code |= 2; diff -= step >> 1
        if diff >= step >> 2: code |= 1
        predictor, index = advance(predictor, index, code)
        if n & 1: block.append(packed | (code << 4))
        else: packed = code
    return bytes(block), (predictor, index)


def decode_frame(block: bytes):
    if len(block) != BLOCK_BYTES: raise ValueError('expected 164 ADPCM bytes')
    predictor, index, reserved = struct.unpack_from('<hBB', block)
    if index > 88 or reserved: raise ValueError('invalid ADPCM state')
    samples = array('h')
    for packed in block[4:]:
        for code in (packed & 15, packed >> 4):
            predictor, index = advance(predictor, index, code)
            samples.append(predictor)
    if sys.byteorder != 'little': samples.byteswap()
    return samples.tobytes()
