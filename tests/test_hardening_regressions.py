"""Regressions for the defects found in the 0.2.0 review.

Each test fails on the baseline commit and passes after its fix.
"""
import asyncio
import base64
import os
import stat
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from callbox.config import Config
from callbox.db import Database
from callbox.main import create_app


# --- F1: the WebSocket scope must get the same guards as HTTP -------------

def test_websocket_rejects_disallowed_host(client):
    """DNS rebinding was blocked on HTTP but not on the gateway, because
    BaseHTTPMiddleware never sees a websocket scope. Pre-fix, the handshake
    below is accepted and answers the hello instead of disconnecting."""
    assert client.get('/api/workspace', headers={'host': 'rebind.attacker.invalid'}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/device', headers={
                'host': 'rebind.attacker.invalid',
                'origin': 'http://rebind.attacker.invalid'}) as ws:
            ws.send_json({'type': 'hello', 'protocol': 'callbox.v1',
                          'device_id': 'dev_x', 'token': 'y' * 40})
            ws.receive_json()


def test_websocket_rejects_cross_origin_handshake(client):
    """Pre-existing control. Guards against the LabGuard refactor losing it."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/device', headers={'origin': 'https://attacker.invalid'}) as ws:
            ws.receive_json()


def test_websocket_handshake_is_rate_limited():
    """The gateway used to sit entirely outside the rate limiter."""
    from callbox.limits import LabGuard, GATEWAY_RATE
    guard = LabGuard(None)
    scope = {'type': 'websocket', 'client': ('10.0.0.2', 1), 'headers': [], 'scheme': 'ws'}
    allowed = sum(guard.rate_ok(scope, '/ws/device') for _ in range(GATEWAY_RATE + 20))
    assert allowed == GATEWAY_RATE, 'gateway handshakes were never rate limited'


def test_websocket_with_valid_host_still_connects(client, db):
    device = client.post('/api/devices', json={'name': 'Guard test', 'kind': 'simulator'}).json()
    with client.websocket_connect('/ws/device') as ws:
        ws.send_json({'type': 'hello', 'protocol': 'callbox.v1',
                      'device_id': device['id'], 'token': device['token']})
        assert ws.receive_json()['type'] == 'ready'


# --- F2: counters must not silently cap at the list LIMITs ---------------

def test_summary_counts_beyond_list_limits(db):
    for i in range(205):
        db.new_call('clinic-demo', f'caller {i}')
    summary = db.summary('clinic-demo')
    assert summary['calls'] == 205, 'summary capped at the calls() LIMIT'
    assert summary['active'] == 205


def test_summary_counts_are_workspace_scoped(db):
    db.create_workspace('other-workspace')
    db.new_call('clinic-demo', 'mine')
    db.new_call('other-workspace', 'theirs')
    assert db.summary('clinic-demo')['calls'] == 1
    assert db.summary('other-workspace')['calls'] == 1


# --- F5: ending a call must not discard a lock others may hold -----------

def test_end_call_keeps_the_turn_lock(config, db):
    app = create_app(config, db)
    agent = app.state.agent
    with TestClient(app) as client:
        client.post('/api/auth/demo')
        cid = client.post('/api/calls', json={'label': 'Lock test'}).json()['id']
        held = agent.lock(cid)
        assert client.post(f'/api/calls/{cid}/end').status_code == 200
        assert agent.lock(cid) is held, 'a waiting turn would keep a lock nobody else acquires'


# --- F6: one client must not be able to reset everyone's rate budget -----

def test_rate_limiter_evicts_only_stale_windows():
    from callbox.limits import LabGuard  # imported here so the pre-fix tree still collects
    guard = LabGuard(None)
    scope = {'type': 'http', 'client': ('10.0.0.1', 1), 'headers': [], 'method': 'GET'}
    for _ in range(10):
        guard.rate_ok(scope, '/api/calls')
    victim = guard.buckets[('10.0.0.1', 'api')]
    for i in range(2100):
        guard.rate_ok({**scope, 'client': (f'10.9.{i // 256}.{i % 256}', 1)}, '/api/calls')
    assert guard.buckets.get(('10.0.0.1', 'api')) == victim, 'flooding reset another client’s counter'
    assert len(guard.buckets) > 0


# --- F10: the runtime directory holds transcripts ------------------------

def test_runtime_directory_is_not_world_readable(tmp_path, monkeypatch):
    monkeypatch.setenv('CALLBOX_DATA_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('CALLBOX_ADMIN_TOKEN', '')
    monkeypatch.setenv('CALLBOX_DEMO', '1')
    config = Config.from_env()
    mode = stat.S_IMODE(os.stat(config.data_dir).st_mode)
    assert not mode & 0o077, f'.runtime is group/world accessible: {mode:o}'
    token_file = config.data_dir / 'admin-token'
    assert not stat.S_IMODE(os.stat(token_file).st_mode) & 0o077


# --- F3: resampling must not stall the event loop ------------------------

def test_resample_produces_correct_rate_conversions():
    from callbox.audio import resample_pcm16, SAMPLE_RATE
    import array as _array
    for rate, expected_ratio in ((48000, 1 / 3), (24000, 2 / 3), (SAMPLE_RATE, 1.0)):
        samples = _array.array('h', [(i % 200) - 100 for i in range(rate)])
        out = _array.array('h'); out.frombytes(resample_pcm16(samples.tobytes(), rate))
        assert abs(len(out) - rate * expected_ratio) <= 2, f'{rate} Hz produced {len(out)} samples'


def test_resample_does_not_block_the_event_loop():
    """Pre-fix this ran inline in gateway.process_turn, so a long reply froze
    every other call, the ping heartbeat and the rate window."""
    import time as _time
    from callbox.audio import resample_async, to_wav

    # 20 s of 48 kHz audio: enough work that inline execution is obvious.
    payload = to_wav(bytes(48000 * 2 * 20), sample_rate=48000)

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        started = _time.perf_counter()
        out = await resample_async(payload)
        elapsed = _time.perf_counter() - started
        beat.cancel()
        return ticks, elapsed, len(out)

    ticks, elapsed, size = asyncio.run(run())
    assert size > 0
    # The heartbeat must have kept running while the resampler worked.
    assert ticks >= max(1, int(elapsed / 0.02)), f'loop was starved: {ticks} ticks in {elapsed:.2f}s'


# --- F4: the gateway must not hit SQLite twice per audio frame -----------

def test_gateway_batches_device_writes(client, db, monkeypatch):
    """Frame totals must stay exact while the per-frame writes go away."""
    import callbox.db as db_module
    writes = []
    original = db_module.Database.touch_device

    def counting(self, did, online=True, frames=0):
        writes.append(frames)
        return original(self, did, online=online, frames=frames)

    monkeypatch.setattr(db_module.Database, 'touch_device', counting)
    device = client.post('/api/devices', json={'name': 'Batch test', 'kind': 'simulator'}).json()
    frame = {'type': 'audio', 'sample_rate': 16000, 'channels': 1,
             'pcm16': base64.b64encode(bytes(640)).decode()}
    with client.websocket_connect('/ws/device') as ws:
        ws.send_json({'type': 'hello', 'protocol': 'callbox.v1',
                      'device_id': device['id'], 'token': device['token']})
        assert ws.receive_json()['type'] == 'ready'
        ws.send_json({'type': 'call.start', 'mode': 'echo', 'consent': True})
        cid = ws.receive_json()['call_id']
        for seq in range(30):
            ws.send_json({**frame, 'call_id': cid, 'seq': seq, 'epoch': 0})
            assert ws.receive_json()['type'] == 'audio.output'
        ws.send_json({'type': 'call.end', 'call_id': cid})
        assert ws.receive_json()['type'] == 'call.ended'
    assert db.devices('clinic-demo')[0]['frames'] == 30, 'batching lost frames'
    assert len(writes) < 30, f'still writing per frame: {len(writes)} writes for 30 frames'
