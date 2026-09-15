"""Multi-tenancy: number routing, per-tenant identity, and isolation.

Isolation is the load-bearing property. One tenant reading or writing
another's data is the failure that ends the product, so it is tested from
several directions rather than assumed from the WHERE clauses.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from callbox.errors import AppError
from callbox.main import create_app
from callbox.orchestrator import Orchestrator, ToolContext
from callbox.tenancy import NumberService, normalise


@pytest.fixture
def numbers(db):
    return NumberService(db)


@pytest.fixture
def two_tenants(db, numbers):
    db.create_workspace('clinic-a', name='Aarogya Clinic', language='hinglish', voice='shimmer')
    db.create_workspace('clinic-b', name='Sunrise Dental', language='en', voice='alloy')
    numbers.assign('clinic-a', '+918000000001', 'exotel', status='active')
    numbers.assign('clinic-b', '+918000000002', 'plivo', status='active')
    return 'clinic-a', 'clinic-b'


# --- number normalisation ----------------------------------------------

def test_the_shapes_a_carrier_actually_sends_all_converge():
    """Inbound webhooks and SIP headers are inconsistent for one number."""
    for shape in ('+918012345678', '918012345678', '08012345678',
                  '8012345678', '080-1234-5678', ' +91 80 1234 5678 '):
        assert normalise(shape) == '+918012345678', f'{shape!r} did not normalise'


def test_rubbish_is_refused_not_guessed():
    for junk in ('12345', '', '   ', 'not a number', None, 'sip:evil@host', '+0123'):
        assert normalise(junk) is None, f'{junk!r} should not produce a number'


# --- routing ------------------------------------------------------------

def test_the_dialled_number_selects_the_tenant(numbers, two_tenants):
    assert numbers.resolve('+918000000001') == 'clinic-a'
    assert numbers.resolve('08000000002') == 'clinic-b'


def test_an_unrouted_number_is_refused_not_defaulted(numbers, two_tenants):
    """Answering a stranger's call with someone else's business identity, and
    writing to their database, is worse than not answering."""
    assert numbers.resolve('+919999999999') is None
    with pytest.raises(AppError) as error:
        numbers.resolve_or_raise('+919999999999')
    assert error.value.code == 'unrouted_number'


def test_an_inactive_number_does_not_route(numbers, db):
    db.create_workspace('clinic-c', name='Pending Clinic')
    numbers.assign('clinic-c', '+918000000003', 'plivo')          # pending_kyc
    assert numbers.resolve('+918000000003') is None, 'KYC is not optional'
    numbers.activate('+918000000003')
    assert numbers.resolve('+918000000003') == 'clinic-c'


def test_one_number_cannot_serve_two_tenants(numbers, two_tenants):
    with pytest.raises(AppError) as error:
        numbers.assign('clinic-b', '+918000000001', 'plivo')
    assert error.value.code == 'number_taken'


def test_numbers_are_validated_on_assignment(numbers, two_tenants):
    with pytest.raises(AppError) as e:
        numbers.assign('clinic-a', 'nonsense', 'exotel')
    assert e.value.code == 'bad_number'
    with pytest.raises(AppError) as e:
        numbers.assign('clinic-a', '+918000000009', 'not-a-provider')
    assert e.value.code == 'bad_provider'


# --- caller ID ----------------------------------------------------------

def test_outbound_requires_an_approved_verified_number(numbers, two_tenants):
    """India in particular will not present an unauthorised caller ID, so
    refuse locally rather than have a carrier reject or rewrite the call."""
    with pytest.raises(AppError) as e:
        numbers.caller_id('clinic-a')
    assert e.value.code == 'no_outbound_number'

    numbers.activate('+918000000001', caller_id_verified=False, outbound=True)
    with pytest.raises(AppError) as e:
        numbers.caller_id('clinic-a')
    assert e.value.code == 'caller_id_unverified'

    numbers.activate('+918000000001', caller_id_verified=True, outbound=True)
    assert numbers.caller_id('clinic-a') == '+918000000001'


def test_each_tenant_presents_its_own_caller_id(numbers, two_tenants):
    numbers.activate('+918000000001', caller_id_verified=True, outbound=True)
    numbers.activate('+918000000002', caller_id_verified=True, outbound=True)
    assert numbers.caller_id('clinic-a') == '+918000000001'
    assert numbers.caller_id('clinic-b') == '+918000000002'


# --- isolation ----------------------------------------------------------

def test_tenants_cannot_see_each_others_records(db, two_tenants):
    a, b = two_tenants
    db.new_call(a, 'Caller A')
    db.new_call(b, 'Caller B')
    assert [c['label'] for c in db.calls(a)] == ['Caller A']
    assert [c['label'] for c in db.calls(b)] == ['Caller B']
    assert db.summary(a)['calls'] == 1 and db.summary(b)['calls'] == 1


def test_a_tool_cannot_reach_another_tenants_data(config, db, two_tenants):
    """The orchestrator carries the tenant in its context; a tool run for one
    tenant must be unable to touch the other."""
    a, b = two_tenants
    orch = Orchestrator(db, config)
    call_b = db.new_call(b, 'Caller B')
    ctx_b = ToolContext(workspace=b, call_id=call_b['id'])

    from datetime import datetime, timedelta
    from callbox.db import IST
    date = str(datetime.now(IST).date() + timedelta(days=1))
    slots = asyncio.run(orch.execute('get_available_slots', {'date': date}, ctx_b))['slots']
    asyncio.run(orch.execute('hold_slot',
                             {'starts_at': slots[0]['starts_at'], 'name': 'B Patient'}, ctx_b))
    asyncio.run(orch.execute('confirm_booking', {}, ctx_b))

    assert len(db.appointments(b)) == 1
    assert db.appointments(a) == [], "tenant A must not see tenant B's booking"

    ctx_a = ToolContext(workspace=a, call_id=db.new_call(a, 'Caller A')['id'],
                        verified=True, patient_ref='B Patient')
    found = asyncio.run(orch.execute('find_my_appointments', {}, ctx_a))
    assert found['appointments'] == [], 'verification in one tenant must not leak another'


def test_a_call_id_from_another_tenant_is_rejected(db, two_tenants):
    a, b = two_tenants
    call_b = db.new_call(b, 'Caller B')
    with pytest.raises(AppError) as error:
        db.call(a, call_b['id'])
    assert error.value.code == 'call_missing'


def test_same_slot_can_be_booked_by_different_tenants(db, two_tenants):
    """The unique index is per workspace: two clinics share a wall clock, not
    a calendar."""
    from datetime import datetime, timedelta
    from callbox.db import IST
    date = str(datetime.now(IST).date() + timedelta(days=1))
    a, b = two_tenants
    slot = db.slots(a, date)[0]['starts_at']
    db.book(a, db.new_call(a, 'A')['id'], 'Patient A', slot)
    db.book(b, db.new_call(b, 'B')['id'], 'Patient B', slot)
    assert len(db.appointments(a)) == 1 and len(db.appointments(b)) == 1


# --- per-tenant identity ------------------------------------------------

def test_each_tenant_has_its_own_voice_and_language(db, two_tenants):
    a, b = two_tenants
    assert db.settings(a)['language'] == 'hinglish' and db.settings(a)['voice'] == 'shimmer'
    assert db.settings(b)['language'] == 'en' and db.settings(b)['voice'] == 'alloy'


def test_the_agent_prompt_reflects_the_tenant(config, db, two_tenants):
    from callbox.realtime import RealtimeSession
    config.api_key = 'realtime-test-not-a-real-key'
    a, b = two_tenants
    for workspace, expect_name, expect_voice in ((a, 'Aarogya Clinic', 'shimmer'),
                                                 (b, 'Sunrise Dental', 'alloy')):
        call = db.new_call(workspace, 'x', provider='openai', consent=True)
        session = RealtimeSession(db, None, config, workspace, call['id'])
        built = session.session_config()['session']
        assert expect_name in built['instructions']
        assert built['audio']['output']['voice'] == expect_voice
    hinglish = RealtimeSession(db, None, config, a,
                               db.new_call(a, 'y', provider='openai', consent=True)['id'])
    assert 'Hinglish' in hinglish.session_config()['session']['instructions']


# --- platform API -------------------------------------------------------

def test_platform_endpoints_require_the_admin_token(config, db):
    app = create_app(config, db)
    with TestClient(app) as client:
        assert client.post('/api/auth/demo').status_code == 200
        # A signed-in tenant session is deliberately not enough.
        assert client.get('/api/platform/tenants').status_code == 403
        assert client.post('/api/platform/tenants',
                           json={'id': 'sneaky', 'name': 'Sneaky'}).status_code == 403
        ok = client.get('/api/platform/tenants',
                        headers={'Authorization': 'Bearer ' + config.admin_token})
        assert ok.status_code == 200


def test_platform_can_provision_a_tenant_end_to_end(config, db):
    app = create_app(config, db)
    auth = {'Authorization': 'Bearer ' + config.admin_token}
    with TestClient(app) as client:
        created = client.post('/api/platform/tenants', headers=auth,
                              json={'id': 'clinic-x', 'name': 'Clinic X',
                                    'language': 'hinglish', 'voice': 'shimmer'})
        assert created.status_code == 201, created.text
        assigned = client.post('/api/platform/tenants/clinic-x/numbers', headers=auth,
                               json={'number': '08000000077', 'provider': 'exotel'})
        assert assigned.status_code == 201
        assert assigned.json()['e164'] == '+918000000077'
        # Pending KYC does not route yet.
        assert client.get('/api/platform/route', headers=auth,
                          params={'called': '+918000000077'}).status_code == 404
        client.post('/api/platform/numbers/+918000000077/activate', headers=auth,
                    json={'caller_id_verified': True, 'outbound': True})
        routed = client.get('/api/platform/route', headers=auth,
                            params={'called': '08000000077'})
        assert routed.status_code == 200 and routed.json()['tenant'] == 'clinic-x'
