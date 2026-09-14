"""The orchestrator is the layer the voice model must not be able to skip."""
import asyncio

import pytest

from callbox.orchestrator import FAST, MEDIUM, OPEN, VERIFIED, Orchestrator, Tool, ToolContext, obj


@pytest.fixture
def orch(config, db):
    return Orchestrator(db, config)


@pytest.fixture
def ctx(db):
    call = db.new_call('clinic-demo', 'Orchestrator caller', provider='openai', consent=True)
    return ToolContext(workspace='clinic-demo', call_id=call['id'], channel='sip')


def run(orch, name, args, ctx):
    return asyncio.run(orch.execute(name, args, ctx))


def tomorrow():
    from datetime import datetime, timedelta
    from callbox.db import IST
    return str(datetime.now(IST).date() + timedelta(days=1))


# --- risk tiers ---------------------------------------------------------

def test_unverified_caller_cannot_read_existing_records(orch, ctx):
    """Caller ID is not identity: spoofable, shared, and the same for every
    call from the clinic's own reception phone."""
    result = run(orch, 'find_my_appointments', {}, ctx)
    assert result['error'] == 'verification_required'


def test_unverified_caller_cannot_cancel(orch, ctx):
    result = run(orch, 'cancel_my_appointment', {'appointment_id': 'apt_whatever'}, ctx)
    assert result['error'] == 'verification_required'


def test_booking_a_new_appointment_needs_no_verification(orch, ctx, db):
    """A new booking under a name the caller supplies discloses nothing."""
    slots = run(orch, 'get_available_slots', {'date': tomorrow()}, ctx)['slots']
    run(orch, 'hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'Priya Sharma'}, ctx)
    assert run(orch, 'confirm_booking', {}, ctx)['committed'] is True
    assert len(db.appointments('clinic-demo')) == 1


def test_verified_caller_only_sees_their_own(orch, ctx, db):
    slots = run(orch, 'get_available_slots', {'date': tomorrow()}, ctx)['slots']
    for name, slot in (('Priya Sharma', slots[0]), ('Someone Else', slots[1])):
        run(orch, 'hold_slot', {'starts_at': slot['starts_at'], 'name': name}, ctx)
        run(orch, 'confirm_booking', {}, ctx)
    ctx.verified, ctx.patient_ref = True, 'Priya Sharma'
    rows = run(orch, 'find_my_appointments', {}, ctx)['appointments']
    assert [r['name'] for r in rows] == ['Priya Sharma']


def test_verified_caller_cannot_cancel_someone_elses(orch, ctx, db):
    slots = run(orch, 'get_available_slots', {'date': tomorrow()}, ctx)['slots']
    run(orch, 'hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'Someone Else'}, ctx)
    other = run(orch, 'confirm_booking', {}, ctx)['appointment']['id']
    ctx.verified, ctx.patient_ref = True, 'Priya Sharma'
    result = run(orch, 'cancel_my_appointment', {'appointment_id': other}, ctx)
    assert result['error'] == 'not_your_appointment'
    assert db.appointments('clinic-demo')[0]['status'] == 'confirmed'


def test_verification_refuses_rather_than_claiming_a_message_was_sent(orch, ctx):
    """No messaging channel is connected, so the model must not tell a caller
    that a code was sent."""
    result = run(orch, 'request_verification_code', {}, ctx)
    assert result['sent'] is False
    assert result['reason'] == 'no_messaging_channel_connected'
    assert 'do not claim' in result['message'].lower()


# --- argument validation ------------------------------------------------

def test_unknown_tool_is_refused(orch, ctx):
    assert run(orch, 'delete_all_patients', {}, ctx)['error'] == 'unknown_tool'


def test_unknown_argument_is_refused(orch, ctx):
    result = run(orch, 'get_available_slots', {'date': tomorrow(), 'admin': True}, ctx)
    assert result['error'] == 'unknown_argument'


def test_missing_argument_is_refused(orch, ctx):
    assert run(orch, 'get_available_slots', {}, ctx)['error'] == 'missing_argument'


def test_wrong_type_is_refused(orch, ctx):
    assert run(orch, 'get_available_slots', {'date': 20260915}, ctx)['error'] == 'bad_argument'


def test_a_raising_tool_never_escapes(orch, ctx):
    orch.register(Tool('explode', 'boom', obj(), lambda a, c: 1 / 0))
    assert run(orch, 'explode', {}, ctx)['error'] == 'tool_failed'


# --- concurrency --------------------------------------------------------

def test_writes_are_serialised_not_parallel(orch, ctx):
    """parallel_tool_calls is accepted by the API, but two concurrent
    mutations is the race the unique index exists to catch."""
    order = []

    async def slow_write(args, c):
        order.append('enter')
        await asyncio.sleep(0.05)
        order.append('exit')
        return {'ok': True}

    orch.register(Tool('slow_write', 'x', obj(), slow_write, writes=True))

    async def both():
        await asyncio.gather(orch.execute('slow_write', {}, ctx),
                             orch.execute('slow_write', {}, ctx))
    asyncio.run(both())
    assert order == ['enter', 'exit', 'enter', 'exit'], f'writes overlapped: {order}'


def test_reads_may_overlap(orch, ctx):
    active, peak = [0], [0]

    async def slow_read(args, c):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.05)
        active[0] -= 1
        return {'ok': True}

    orch.register(Tool('slow_read', 'x', obj(), slow_read, writes=False))

    async def both():
        await asyncio.gather(orch.execute('slow_read', {}, ctx),
                             orch.execute('slow_read', {}, ctx))
    asyncio.run(both())
    assert peak[0] == 2, 'reads should not be serialised'


def test_hold_then_confirm_is_ordered(orch, ctx, db):
    slots = run(orch, 'get_available_slots', {'date': tomorrow()}, ctx)['slots']
    assert run(orch, 'confirm_booking', {}, ctx)['error'] == 'nothing_held'
    run(orch, 'hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'Priya Sharma'}, ctx)
    assert run(orch, 'confirm_booking', {}, ctx)['committed'] is True
    # The hold is consumed, so a repeat cannot double-book.
    assert run(orch, 'confirm_booking', {}, ctx)['error'] == 'nothing_held'
    assert len(db.appointments('clinic-demo')) == 1


# --- phase routing ------------------------------------------------------

def test_phases_publish_only_relevant_tools(orch):
    reception = {t['name'] for t in orch.published('reception')}
    records = {t['name'] for t in orch.published('records')}
    assert 'get_fee' in reception and 'cancel_my_appointment' not in reception
    assert 'cancel_my_appointment' in records
    # 82 tools are accepted by the API without error, so over-exposure fails
    # silently as worse selection rather than as a rejection.
    assert len(reception) < len(orch.tools)


def test_every_published_tool_has_a_valid_schema(orch):
    for schema in orch.published('*'):
        assert schema['type'] == 'function'
        assert schema['name'] and schema['description']
        assert schema['parameters']['type'] == 'object'


# --- audit --------------------------------------------------------------

def test_every_call_is_audited(orch, ctx, db):
    run(orch, 'get_fee', {}, ctx)
    run(orch, 'delete_all_patients', {}, ctx)
    kinds = [e['kind'] for e in db.events('clinic-demo')]
    assert 'tool.get_fee' in kinds
    assert 'tool.delete_all_patients' in kinds, 'a refused call must still be recorded'


def test_results_carry_timing_for_latency_budgeting(orch, ctx):
    assert isinstance(run(orch, 'get_fee', {}, ctx)['elapsed_ms'], float)


def test_latency_classes_are_declared(orch):
    assert orch.tools['get_fee'].latency == FAST
    assert orch.tools['confirm_booking'].latency == MEDIUM
    assert orch.tools['confirm_booking'].writes is True
    assert orch.tools['get_available_slots'].writes is False
    assert orch.tools['find_my_appointments'].risk == VERIFIED
    assert orch.tools['get_business_hours'].risk == OPEN


# --- phase transitions --------------------------------------------------

def test_checking_availability_opens_the_booking_phase(orch, ctx):
    """Routing that only restricts strands the call. Found end-to-end: the
    model correctly reported it could not book, because hold_slot and
    confirm_booking were never published to the phase it was stuck in."""
    assert ctx.phase == 'reception'
    assert 'hold_slot' not in {t['name'] for t in orch.published(ctx.phase)}
    run(orch, 'get_available_slots', {'date': tomorrow()}, ctx)
    assert ctx.phase == 'booking'
    assert 'hold_slot' in {t['name'] for t in orch.published(ctx.phase)}


def test_a_completed_booking_returns_to_reception(orch, ctx):
    slots = run(orch, 'get_available_slots', {'date': tomorrow()}, ctx)['slots']
    run(orch, 'hold_slot', {'starts_at': slots[0]['starts_at'], 'name': 'Priya Sharma'}, ctx)
    run(orch, 'confirm_booking', {}, ctx)
    assert ctx.phase == 'reception', 'the call should not stay stuck in booking'


def test_a_failed_tool_does_not_advance_the_phase(orch, ctx):
    run(orch, 'get_available_slots', {'date': 'not-a-date'}, ctx)
    assert ctx.phase == 'reception'


def test_no_phase_is_a_dead_end(orch):
    """Phases are additive. Found end-to-end twice: a caller who asked about
    existing records moved into 'records' and could then no longer book,
    because the phase sets were exclusive. A caller changes subject whenever
    they like."""
    entry = {'get_business_hours', 'get_fee', 'get_location',
             'get_available_slots', 'create_staff_request', 'request_verification_code'}
    for phase in ('reception', 'booking', 'records'):
        names = {t['name'] for t in orch.published(phase)}
        assert entry <= names, f'{phase} strands the caller: missing {entry - names}'


def test_each_phase_still_narrows_what_is_published(orch):
    """Additive must not mean publishing everything: 82 tools are accepted by
    the API without error, so over-exposure degrades selection silently."""
    for phase in ('reception', 'booking', 'records'):
        assert len(orch.published(phase)) < len(orch.tools)
    assert 'hold_slot' not in {t['name'] for t in orch.published('reception')}
    assert 'cancel_my_appointment' not in {t['name'] for t in orch.published('booking')}
