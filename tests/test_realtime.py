"""The realtime model owns speech. It must never own the data.

Every fact and every write goes through a tool handled against the same
deterministic backend the typed console uses.
"""
import pytest
from callbox.config import Config
from callbox.db import Database
from callbox.realtime import INSTRUCTIONS, RealtimeSession


import asyncio


@pytest.fixture
def session(config, db):
    config.api_key = 'realtime-test-not-a-real-key'
    call = db.new_call('clinic-demo', 'Realtime caller', provider='openai', consent=True)
    session = RealtimeSession(db, None, config, 'clinic-demo', call['id'])
    # dispatch is async now that every call goes through the orchestrator.
    session.dispatch = lambda name, args: asyncio.run(
        RealtimeSession.dispatch(session, name, args))
    return session


def test_realtime_requires_an_openai_key(config):
    config.api_key = ''
    config.openrouter_key = 'an-openrouter-key'
    assert not config.realtime_available, 'OpenRouter cannot proxy the Realtime API'
    config.api_key = 'an-openai-key'
    assert config.realtime_available


def test_booking_needs_a_hold_first(session):
    """The model cannot book by calling confirm_booking on its own."""
    result = session.dispatch('confirm_booking', {})
    assert result['error'] == 'nothing_held'
    assert not session.db.appointments('clinic-demo')


def test_hold_does_not_book(session):
    slots = session.dispatch('get_available_slots', {'date': _tomorrow()})['slots']
    held = session.dispatch('hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'Priya Sharma'})
    assert held['held'] is True and held['booked'] is False
    assert not session.db.appointments('clinic-demo'), 'hold_slot must not commit anything'


def test_hold_then_confirm_commits_once(session):
    slots = session.dispatch('get_available_slots', {'date': _tomorrow()})['slots']
    session.dispatch('hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'Priya Sharma'})
    committed = session.dispatch('confirm_booking', {})
    assert committed['committed'] is True
    rows = session.db.appointments('clinic-demo')
    assert len(rows) == 1 and rows[0]['name'] == 'Priya Sharma'
    # The hold is consumed: a repeated confirm cannot double-book.
    again = session.dispatch('confirm_booking', {})
    assert again['error'] == 'nothing_held'
    assert len(session.db.appointments('clinic-demo')) == 1


def test_model_cannot_invent_a_slot(session):
    """A time the calendar never offered must be refused."""
    result = session.dispatch('hold_slot', {'starts_at': f'{_tomorrow()}T03:00:00+05:30', 'name': 'Ghost'})
    assert result['error'] == 'slot_unavailable'
    assert not session.db.appointments('clinic-demo')


def test_hold_validates_the_name(session):
    slots = session.dispatch('get_available_slots', {'date': _tomorrow()})['slots']
    result = session.dispatch('hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'x'})
    assert result['error'] == 'name'


def test_the_model_cannot_reach_a_tool_the_phase_does_not_publish(session):
    published = {t['name'] for t in session.orchestrator.published('reception')}
    assert 'cancel_my_appointment' not in published


def test_reads_come_from_the_database(session, db):
    db.set_settings('clinic-demo', {**db.settings('clinic-demo'), 'fee': 777, 'open_hour': 9})
    assert session.dispatch('get_fee', {})['fee_inr'] == 777
    assert session.dispatch('get_business_hours', {})['open_hour'] == 9


def test_staff_request_never_claims_a_transfer(session):
    result = session.dispatch('create_staff_request', {'reason': 'Caller asked for a person'})
    assert result['queued'] is True
    assert 'not a transfer' in result['note'].lower()
    assert [t for t in session.db.tasks('clinic-demo') if t['status'] == 'open']


def test_unknown_tool_is_refused(session):
    assert session.dispatch('send_whatsapp', {'to': '123'})['error'] == 'unknown_tool'


def test_tool_failures_never_raise(session):
    """A raising tool would drop the websocket mid-call."""
    for name, args in [('get_available_slots', {'date': 'not-a-date'}),
                       ('get_available_slots', {}),
                       ('hold_slot', {}),
                       ('create_staff_request', {})]:
        result = session.dispatch(name, args)
        assert isinstance(result, dict)


def test_every_published_tool_is_handled(session):
    for schema in session.orchestrator.published('*'):
        assert session.dispatch(schema['name'], {}) is not None, \
            f"{schema['name']} is published to the model but not handled"


def test_instructions_forbid_the_unimplemented_capabilities():
    text = INSTRUCTIONS.lower()
    for rule in ('transfer', 'sms', 'whatsapp', 'external calendar',
                 'medical advice', 'emergency services', 'never say an appointment is booked'):
        assert rule in text, f'the system prompt must address {rule!r}'


def _tomorrow():
    from datetime import datetime, timedelta
    from callbox.db import IST
    return str(datetime.now(IST).date() + timedelta(days=1))
