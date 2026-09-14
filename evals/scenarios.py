"""The thirteen cases named in docs/ROADMAP.md, executed against the real app.

Each scenario scores three things, per the roadmap's own instruction to
"preserve ground truth and show what really committed":

  reply         the assistant said the right kind of thing
  ground_truth  what is actually in SQLite afterwards, not what the reply claimed
  honesty       no reply claimed a capability this release does not have

A scenario passes only when every check passes.
"""
import asyncio
import uuid

import httpx
from fastapi.testclient import TestClient

from callbox.config import Config
from callbox.db import Database
from callbox.main import create_app
from callbox.providers import OpenRouterProvider
from evals.honesty import scan

REGISTRY = []


def scenario(key, title, roadmap):
    def wrap(fn):
        fn.key, fn.title, fn.roadmap = key, title, roadmap
        REGISTRY.append(fn)
        return fn
    return wrap


class Result:
    def __init__(self, fn):
        self.key, self.title, self.roadmap = fn.key, fn.title, fn.roadmap
        self.checks = []
        self.ground_truth = {}
        self.replies = []
        self.error = None

    def check(self, name, ok, detail=''):
        self.checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)})
        return ok

    def say(self, text):
        if text:
            self.replies.append(text)

    @property
    def passed(self):
        return self.error is None and bool(self.checks) and all(c['ok'] for c in self.checks)

    def as_dict(self):
        return {'key': self.key, 'title': self.title, 'roadmap': self.roadmap,
                'passed': self.passed, 'error': self.error,
                'checks': self.checks, 'ground_truth': self.ground_truth,
                'replies': self.replies}


class Lab:
    """A fresh server, database and signed-in operator for one scenario."""

    def __init__(self, tmp_path, provider=None, demo=True, token=None):
        self.config = Config(tmp_path, token or ('eval-token-' + 'x' * 40), demo=demo)
        self.db = Database(':memory:', seed=False)
        self.app = create_app(self.config, self.db, provider)
        self.client = TestClient(self.app)

    def __enter__(self):
        self.client.__enter__()
        if self.config.demo:
            assert self.client.post('/api/auth/demo').status_code == 200
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        self.db.close()
        return False

    def start_call(self, **kwargs):
        body = {'label': 'Fictional caller', **kwargs}
        return self.client.post('/api/calls', json=body).json()

    def say(self, cid, text):
        return self.client.post(f'/api/calls/{cid}/turn',
                                json={'text': text, 'request_id': uuid.uuid4().hex})

    def appointments(self):
        return self.db.appointments(self.config.workspace)

    def open_tasks(self):
        return [t for t in self.db.tasks(self.config.workspace) if t['status'] == 'open']

    def call(self, cid):
        return self.db.call(self.config.workspace, cid)


def timeout_provider(config):
    def handle(request):
        raise httpx.TimeoutException('upstream stalled', request=request)
    config.provider_kind = 'openrouter'
    config.openrouter_key = 'eval-not-a-real-key'
    return OpenRouterProvider(config, httpx.MockTransport(handle))


# --- 1 ---------------------------------------------------------------------

@scenario('booking_happy_path', 'End-to-end successful booking',
          'Measure an end-to-end successful booking')
def booking_happy_path(tmp_path):
    r = Result(booking_happy_path)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        for text in ['book tomorrow', '1', 'Mira Demo']:
            r.say(lab.say(cid, text).json()['reply'])
        final = lab.say(cid, 'confirm').json()
        r.say(final['reply'])
        r.check('reply_confirms', 'confirmed' in final['reply'].lower(), final['reply'][:90])
        booked = lab.appointments()
        r.ground_truth = {'appointments': len(booked),
                          'status': booked[0]['status'] if booked else None,
                          'name': booked[0]['name'] if booked else None}
        r.check('one_row_committed', len(booked) == 1, f'{len(booked)} rows')
        r.check('row_is_confirmed', booked and booked[0]['status'] == 'confirmed')
        r.check('name_matches_caller', booked and booked[0]['name'] == 'Mira Demo')
        r.check('tool_action_reported',
                any(a.get('tool') == 'book_appointment' for a in final['actions']))
    return r


# --- 2 ---------------------------------------------------------------------

@scenario('racing_slot_conflict', 'Two callers race for the same slot',
          'a racing slot conflict')
def racing_slot_conflict(tmp_path):
    r = Result(racing_slot_conflict)
    with Lab(tmp_path) as lab:
        async def run():
            transport = httpx.ASGITransport(app=lab.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver',
                                         headers={'Authorization': 'Bearer ' + lab.config.admin_token}) as c:
                calls = []
                for who in ('Caller One', 'Caller Two'):
                    cid = (await c.post('/api/calls', json={'label': who})).json()['id']
                    for text in ['book tomorrow', '1', who.replace(' ', ' ')]:
                        await c.post(f'/api/calls/{cid}/turn',
                                     json={'text': text, 'request_id': uuid.uuid4().hex})
                    calls.append(cid)
                # Both confirm the same slot at the same time.
                return await asyncio.gather(*(
                    c.post(f'/api/calls/{cid}/turn',
                           json={'text': 'confirm', 'request_id': uuid.uuid4().hex})
                    for cid in calls))

        responses = asyncio.run(run())
        for response in responses:
            r.say(response.json().get('reply'))
        booked = [a for a in lab.appointments() if a['status'] == 'confirmed']
        r.ground_truth = {'confirmed_rows': len(booked),
                          'http_statuses': [x.status_code for x in responses]}
        r.check('exactly_one_booking', len(booked) == 1, f'{len(booked)} confirmed rows')
        r.check('loser_told_to_rechoose',
                sum('no longer available' in (x.json().get('reply') or '').lower()
                    or 'just taken' in (x.json().get('reply') or '').lower()
                    for x in responses) == 1)
        r.check('no_false_confirmation_to_both',
                sum('confirmed in the local calendar' in (x.json().get('reply') or '').lower()
                    for x in responses) == 1)
        # The checks above exercise the availability recheck. In-process they
        # cannot reach the unique index, because db.book serialises on one
        # RLock, so the index is asserted directly: it is what protects a
        # multi-process deployment where that lock does not apply.
        import sqlite3
        from callbox.db import ident, now_iso
        taken = booked[0]
        duplicate_rejected = False
        try:
            lab.db.execute('INSERT INTO appointments VALUES(?,?,?,?,?,?,?,?)',
                           (ident('apt'), lab.config.workspace, None, 'Duplicate Writer',
                            taken['starts_at'], taken['duration'], 'confirmed', now_iso()))
        except sqlite3.IntegrityError:
            duplicate_rejected = True
        r.check('unique_index_blocks_a_second_confirmed_row', duplicate_rejected,
                'a concurrent process could otherwise double-book the slot')
        r.check('still_one_row_after_the_attempt',
                len([a for a in lab.appointments() if a['status'] == 'confirmed']) == 1)
    return r


# --- 3 ---------------------------------------------------------------------

@scenario('duplicate_confirmation', 'A retried confirmation books once',
          'duplicate confirmation')
def duplicate_confirmation(tmp_path):
    r = Result(duplicate_confirmation)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        for text in ['book tomorrow', '1', 'Mira Demo']:
            lab.say(cid, text)
        rid = 'duplicate-confirm-0001'
        first = lab.client.post(f'/api/calls/{cid}/turn', json={'text': 'confirm', 'request_id': rid}).json()
        second = lab.client.post(f'/api/calls/{cid}/turn', json={'text': 'confirm', 'request_id': rid}).json()
        r.say(first['reply']); r.say(second['reply'])
        booked = [a for a in lab.appointments() if a['status'] == 'confirmed']
        r.ground_truth = {'confirmed_rows': len(booked),
                          'first_replayed': first.get('replayed'),
                          'second_replayed': second.get('replayed')}
        r.check('one_row_only', len(booked) == 1, f'{len(booked)} rows')
        r.check('retry_marked_replayed', second.get('replayed') is True)
        r.check('identical_reply', first['reply'] == second['reply'])
        conflict = lab.client.post(f'/api/calls/{cid}/turn',
                                   json={'text': 'different text', 'request_id': rid})
        r.check('reused_id_with_new_payload_rejected', conflict.status_code == 409,
                conflict.status_code)
    return r


# --- 4 ---------------------------------------------------------------------

@scenario('unclear_speech', 'An unparseable request is escalated, not guessed',
          'unclear speech')
def unclear_speech(tmp_path):
    r = Result(unclear_speech)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        reply = lab.say(cid, 'wubble frotz gnnn').json()
        r.say(reply['reply'])
        r.ground_truth = {'intent': reply['intent'], 'open_tasks': len(lab.open_tasks()),
                          'appointments': len(lab.appointments())}
        r.check('intent_is_unknown', reply['intent'] == 'unknown', reply['intent'])
        r.check('escalated_to_staff', len(lab.open_tasks()) == 1)
        r.check('nothing_booked', not lab.appointments())
        r.check('no_invented_answer', 'not sure' in reply['reply'].lower())
    return r


# --- 5 ---------------------------------------------------------------------

@scenario('mixed_language_date', 'A Hinglish date resolves in India time',
          'mixed-language date')
def mixed_language_date(tmp_path):
    from datetime import date, timedelta
    from callbox.db import IST
    from datetime import datetime
    r = Result(mixed_language_date)
    with Lab(tmp_path) as lab:
        today = datetime.now(IST).date()
        for phrase, offset in (('kal appointment chahiye', 1), ('parso book karna hai', 2)):
            cid = lab.start_call(language='hinglish')['id']
            reply = lab.say(cid, phrase).json()
            r.say(reply['reply'])
            expected = str(today + timedelta(days=offset))
            state = lab.call(cid)['state']
            r.check(f'{phrase.split()[0]}_resolves_to_{offset}_days',
                    state.get('date') == expected or expected in reply['reply'],
                    f"got {state.get('date')}, wanted {expected}")
        r.ground_truth = {'timezone': 'Asia/Kolkata', 'today_ist': str(today)}
    return r


# --- 6 ---------------------------------------------------------------------

@scenario('changed_mind', 'Cancelling mid-booking commits nothing',
          'changed mind')
def changed_mind(tmp_path):
    r = Result(changed_mind)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        for text in ['book tomorrow', '1', 'Mira Demo']:
            lab.say(cid, text)
        reply = lab.say(cid, 'cancel').json()
        r.say(reply['reply'])
        r.ground_truth = {'state_after': lab.call(cid)['state'],
                          'appointments': len(lab.appointments())}
        r.check('state_cleared', lab.call(cid)['state'] == {})
        r.check('nothing_booked', not lab.appointments())
        r.check('says_nothing_cancelled_existing',
                'have not been cancelled' in reply['reply'].lower()
                or 'not been cancelled' in reply['reply'].lower())
    return r


# --- 7 ---------------------------------------------------------------------

@scenario('provider_timeout', 'An upstream timeout commits nothing and is not retried',
          'API timeout')
def provider_timeout(tmp_path):
    r = Result(provider_timeout)
    config = Config(tmp_path, 'eval-token-' + 'x' * 40, demo=True)
    provider = timeout_provider(config)
    with Lab(tmp_path, provider=provider) as lab:
        lab.config.provider_kind = 'openrouter'
        lab.config.openrouter_key = 'eval-not-a-real-key'
        cid = lab.start_call(provider='openai', consent=True)['id']
        import math
        import struct
        from callbox.audio import to_wav
        pcm = b''.join(struct.pack('<h', int(6000 * math.sin(2 * math.pi * 440 * t / 16000)))
                       for t in range(16000))
        response = lab.client.post(f'/api/calls/{cid}/audio', content=to_wav(pcm),
                                   headers={'content-type': 'audio/wav',
                                            'x-request-id': 'timeout-case-0001'})
        body = response.json()
        r.ground_truth = {'status': response.status_code, 'code': body.get('error', {}).get('code'),
                          'appointments': len(lab.appointments()),
                          'messages': len(lab.call(cid)['messages'])}
        r.check('reports_timeout', response.status_code == 504, response.status_code)
        r.check('error_code_is_provider_timeout', body.get('error', {}).get('code') == 'provider_timeout')
        r.check('nothing_booked', not lab.appointments())
        r.check('no_caller_turn_recorded', len(lab.call(cid)['messages']) == 1,
                'only the greeting should exist')
        r.check('upstream_body_not_reflected', 'eval-not-a-real-key' not in response.text)
    return r


# --- 8 ---------------------------------------------------------------------

@scenario('hangup_during_commit', 'A hangup before confirming commits nothing',
          'phone hangup during commit')
def hangup_during_commit(tmp_path):
    r = Result(hangup_during_commit)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        for text in ['book tomorrow', '1', 'Mira Demo']:
            lab.say(cid, text)
        lab.client.post(f'/api/calls/{cid}/end')
        late = lab.say(cid, 'confirm')
        r.ground_truth = {'status': late.status_code,
                          'code': late.json().get('error', {}).get('code'),
                          'appointments': len(lab.appointments()),
                          'call_status': lab.call(cid)['status']}
        r.check('call_is_ended', lab.call(cid)['status'] == 'ended')
        r.check('late_confirm_rejected', late.status_code == 409, late.status_code)
        r.check('nothing_booked', not lab.appointments())
    return r


# --- 9 ---------------------------------------------------------------------

@scenario('human_request', 'A request for a person queues a task and claims no transfer',
          'human request')
def human_request(tmp_path):
    r = Result(human_request)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        reply = lab.say(cid, 'I would like a human').json()
        r.say(reply['reply'])
        tasks = lab.open_tasks()
        r.ground_truth = {'intent': reply['intent'], 'open_tasks': len(tasks),
                          'reason': tasks[0]['reason'] if tasks else None}
        r.check('intent_is_human', reply['intent'] == 'human')
        r.check('task_queued', len(tasks) == 1)
        r.check('states_it_is_not_a_transfer', 'not a live transfer' in reply['reply'].lower())
        r.check('no_outgoing_call_claimed', 'no outgoing call' in reply['reply'].lower())
    return r


# --- 10 --------------------------------------------------------------------

@scenario('staff_unavailable', 'An unresolved task stays visible to the operator',
          'staff unavailable')
def staff_unavailable(tmp_path):
    r = Result(staff_unavailable)
    with Lab(tmp_path) as lab:
        cid = lab.start_call()['id']
        lab.say(cid, 'I would like a human')
        lab.client.post(f'/api/calls/{cid}/end')
        summary = lab.client.get('/api/summary').json()
        r.ground_truth = {'review_count': summary['review'], 'open_tasks': len(lab.open_tasks())}
        r.check('task_survives_call_end', len(lab.open_tasks()) == 1)
        r.check('surfaced_in_summary', summary['review'] == 1, summary['review'])
        tid = lab.open_tasks()[0]['id']
        r.check('resolve_requires_confirmation',
                lab.client.post(f'/api/tasks/{tid}/resolve', json={}).status_code == 422)
        r.check('resolves_with_confirmation',
                lab.client.post(f'/api/tasks/{tid}/resolve', json={'confirm': True}).status_code == 200)
        r.check('review_count_drops', lab.client.get('/api/summary').json()['review'] == 0)
    return r


# --- 11 --------------------------------------------------------------------

@scenario('revoked_device', 'Revoking a device disconnects it immediately',
          'revoked device')
def revoked_device(tmp_path):
    r = Result(revoked_device)
    with Lab(tmp_path) as lab:
        device = lab.client.post('/api/devices', json={'name': 'Eval device', 'kind': 'simulator'}).json()
        with lab.client.websocket_connect('/ws/device') as ws:
            ws.send_json({'type': 'hello', 'protocol': 'callbox.v1',
                          'device_id': device['id'], 'token': device['token']})
            r.check('handshake_ready', ws.receive_json()['type'] == 'ready')
            lab.client.post(f"/api/devices/{device['id']}/revoke", json={'confirm': True})
            r.check('socket_closed_on_revoke', ws.receive()['type'] == 'websocket.close')
        r.check('token_no_longer_authenticates',
                lab.db.authenticate_device(device['token'], device['id']) is None)
        r.check('token_never_returned_by_list', device['token'] not in lab.client.get('/api/devices').text)
        r.ground_truth = {'device_status': lab.db.devices(lab.config.workspace)[0]['status'],
                          'revoked': bool(lab.db.devices(lab.config.workspace)[0]['revoked'])}
    return r


# --- 12 --------------------------------------------------------------------

@scenario('network_loss', 'A dropped gateway connection ends its call honestly',
          'network loss')
def network_loss(tmp_path):
    r = Result(network_loss)
    with Lab(tmp_path) as lab:
        device = lab.client.post('/api/devices', json={'name': 'Eval device', 'kind': 'simulator'}).json()
        with lab.client.websocket_connect('/ws/device') as ws:
            ws.send_json({'type': 'hello', 'protocol': 'callbox.v1',
                          'device_id': device['id'], 'token': device['token']})
            ws.receive_json()
            ws.send_json({'type': 'call.start', 'mode': 'echo', 'consent': True})
            cid = ws.receive_json()['call_id']
            # Drop the connection without call.end.
        call = lab.call(cid)
        r.ground_truth = {'call_status': call['status'], 'outcome': call['outcome'],
                          'device_status': lab.db.devices(lab.config.workspace)[0]['status']}
        r.check('call_marked_ended', call['status'] == 'ended', call['status'])
        r.check('outcome_names_the_disconnect', 'disconnect' in call['outcome'].lower(), call['outcome'])
        r.check('device_marked_offline',
                lab.db.devices(lab.config.workspace)[0]['status'] == 'offline')
        r.check('not_reported_as_connected',
                lab.client.get('/api/summary').json()['online'] == 0)
    return r


# --- 13 --------------------------------------------------------------------

@scenario('local_disable', 'With demo mode off nothing is reachable anonymously',
          'local disable')
def local_disable(tmp_path):
    r = Result(local_disable)
    token = 'hardened-eval-token-' + 'z' * 40
    with Lab(tmp_path, demo=False, token=token) as lab:
        r.check('anonymous_api_rejected', lab.client.get('/api/calls').status_code == 401)
        r.check('demo_login_disabled', lab.client.post('/api/auth/demo').status_code == 403)
        r.check('bad_token_rejected',
                lab.client.post('/api/auth/login', json={'token': 'wrong-token-' + 'q' * 20}).status_code == 401)
        signed_in = lab.client.post('/api/auth/login', json={'token': token})
        r.check('correct_token_accepted', signed_in.status_code == 200)
        r.check('session_cookie_is_httponly', 'httponly' in signed_in.headers.get('set-cookie', '').lower())
        r.check('token_not_echoed_in_cookie', token not in signed_in.headers.get('set-cookie', ''))
        r.check('reads_work_once_signed_in', lab.client.get('/api/calls').status_code == 200)
        bootstrap = lab.client.get('/api/bootstrap').json()
        r.ground_truth = {'demo_login_offered': bootstrap['demo_login']}
        r.check('ui_does_not_offer_demo_login', bootstrap['demo_login'] is False)
    return r


def run_all(tmp_root):
    """Every scenario, each in its own directory, with honesty applied to all."""
    results = []
    for fn in REGISTRY:
        path = tmp_root / fn.key
        path.mkdir(parents=True, exist_ok=True)
        result = Result(fn)
        try:
            result = fn(path)
        except Exception as exc:  # a crashing scenario is a failing scenario
            result.error = f'{type(exc).__name__}: {exc}'
        breaches = scan(result.replies)
        result.check('no_unclaimable_capability_asserted', not breaches,
                     '; '.join(f'{claim}: {reply[:60]}' for reply, claim in breaches))
        results.append(result)
    return results
