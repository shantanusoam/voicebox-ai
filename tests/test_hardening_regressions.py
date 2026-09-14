"""Regressions for the defects found in the 0.2.0 review.

Each test fails on the baseline commit and passes after its fix.
"""
import asyncio
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
