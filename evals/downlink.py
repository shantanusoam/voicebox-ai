"""Binary echo evaluation through the real gateway; no hardware or paid calls.

Run separately from the thirteen business-behaviour scenarios. This checks
the negotiated wire format and byte fidelity, not RF delivery during SCO.
"""
import tempfile
from pathlib import Path

from callbox.audio import (FRAME_BYTES, pack_audio_batch, unpack_audio_batch,
                           DOWNLINK_BATCH_HEADER, ADPCM_BATCH_MAGIC)
from callbox.adpcm import BLOCK_BYTES, decode_frame
from evals.scenarios import Lab


def main():
    with tempfile.TemporaryDirectory(prefix='callbox-downlink-') as tmp:
        with Lab(Path(tmp)) as lab:
            device = lab.client.post('/api/devices', json={
                'name': 'Binary echo eval', 'kind': 'simulator'}).json()
            for codec in ('binary', 'adpcm'):
                with lab.client.websocket_connect('/ws/device') as ws:
                    ws.send_json({'type': 'hello', 'protocol': 'callbox.v1',
                                  'device_id': device['id'], 'token': device['token'],
                                  'downlink': codec})
                    assert ws.receive_json()['type'] == 'ready'
                    ws.send_json({'type': 'call.start', 'mode': 'echo', 'consent': True})
                    call_id = ws.receive_json()['call_id']
                    for first in (0, 4, 8):
                        frames = [b'\x12\0' * 320 for _ in range(4)]
                        ws.send_bytes(pack_audio_batch(frames, first))
                        payload = ws.receive_bytes()
                        if codec == 'binary':
                            reply = unpack_audio_batch(payload)
                            assert reply['first_seq'] == first and reply['frames'] == frames
                        else:
                            magic, count, flags, size, seq, epoch = DOWNLINK_BATCH_HEADER.unpack_from(payload)
                            assert (magic, count, flags, size, seq, epoch) == (
                                ADPCM_BATCH_MAGIC, 4, 0, BLOCK_BYTES, first, 0)
                            decoded = [decode_frame(payload[16+i*BLOCK_BYTES:16+(i+1)*BLOCK_BYTES])
                                       for i in range(count)]
                            assert all(len(frame) == FRAME_BYTES for frame in decoded)
                    ws.send_json({'type': 'call.end', 'call_id': call_id})
                    assert ws.receive_json()['type'] == 'call.ended'
    print('PASS negotiated CBB1/CBB2 echo: 12 frames per mode; RF untested')


if __name__ == '__main__':
    main()
