"""Tool orchestration: the layer the voice model is not allowed to skip.

The realtime model decides *what* it wants done. This module decides whether
that is permitted, validates the arguments, serialises anything that writes,
executes it against the deterministic backend, and records what happened. The
model never reaches a database, an API or an MCP server directly.

Three rules earn their place here:

1.  **Risk tiers.** A phone caller is not authenticated. Caller ID is
    spoofable, shared across a household, and identical for every call from
    the clinic's own reception phone - so it can never gate access to someone
    else's record. Tools are therefore split: OPEN tools disclose nothing
    about an existing patient, VERIFIED tools do and require a confirmed
    identity first. Booking a new appointment under a name the caller supplies
    is open; reading or cancelling an existing one is not.

2.  **Writes are serialised.** The Realtime API accepts parallel_tool_calls,
    but two concurrent mutations is precisely the race the one_active_slot
    unique index exists to catch. Reads may overlap; writes take a per-call
    lock in the order the model asked for them, which is what makes
    hold_slot -> confirm_booking a guarantee rather than a hope.

3.  **Phase routing.** 82 tools are accepted by the API without complaint, so
    over-exposure fails silently as worse tool selection and more context per
    turn. Each phase of a call publishes only the handful of tools that phase
    can legitimately need, and tools advance the call to the next phase as it
    progresses. Phases are ADDITIVE, never exclusive: every phase publishes the
    entry-point tools, and a phase adds only the tools that phase unlocks. Two
    separate end-to-end calls proved why. First, restriction without
    advancement stranded the call in 'reception' and the model accurately told
    the caller it could not book. Then, with advancement but exclusive sets, a
    caller who asked about existing records moved into 'records' and could no
    longer book. A caller changes subject whenever they like; the tool list has
    to survive that.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field

from .errors import AppError

OPEN = 'open'            # discloses nothing about an existing patient
VERIFIED = 'verified'    # reads or mutates an existing record

FAST = 'fast'            # answer inline, the caller waits
MEDIUM = 'medium'        # say a filler line first
SLOW = 'slow'            # hand to a workflow, tell the caller you will follow up


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: object
    risk: str = OPEN
    writes: bool = False
    latency: str = FAST
    phases: tuple = ('*',)
    # Phase this tool moves the call into once it succeeds. Routing that only
    # ever restricts is a trap: the call gets stuck in its opening phase and
    # the model truthfully reports that it cannot book.
    advances_to: str | None = None

    def schema(self):
        return {'type': 'function', 'name': self.name,
                'description': self.description, 'parameters': self.parameters}


@dataclass
class ToolContext:
    """Everything policy needs to decide, and audit needs to record."""
    workspace: str
    call_id: str
    channel: str = 'browser'        # browser | sip
    caller_id: str | None = None    # never sufficient for identity on its own
    verified: bool = False
    patient_ref: str | None = None
    phase: str = 'reception'
    state: dict = field(default_factory=dict)


class ToolError(AppError):
    """A refusal the model is expected to explain to the caller."""


def obj(**properties):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(properties)}


class Orchestrator:
    """Registry, policy, execution and audit for everything the model can do."""

    def __init__(self, db, config):
        self.db, self.config = db, config
        self.tools: dict[str, Tool] = {}
        self.write_locks: dict[str, asyncio.Lock] = {}
        self.register_defaults()

    # -- registry ------------------------------------------------------
    def register(self, tool: Tool):
        self.tools[tool.name] = tool
        return tool

    def published(self, phase='*'):
        """Only the tools this phase can legitimately need."""
        return [t.schema() for t in self.tools.values()
                if '*' in t.phases or phase in t.phases]

    def lock_for(self, call_id):
        lock = self.write_locks.get(call_id)
        if lock is None:
            lock = self.write_locks[call_id] = asyncio.Lock()
        return lock

    def release(self, call_id):
        self.write_locks.pop(call_id, None)

    # -- execution -----------------------------------------------------
    async def execute(self, name, arguments, ctx: ToolContext):
        """Authorise, validate, serialise, run, audit. Never raises."""
        started = time.perf_counter()
        tool = self.tools.get(name)
        if tool is None:
            return self.finish(ctx, name, None, started,
                               {'error': 'unknown_tool',
                                'message': 'That action does not exist on this system.'})
        try:
            self.authorise(tool, ctx)
            args = self.validate(tool, arguments or {})
            if tool.writes:
                # Ordered, never concurrent: the confirmation guarantee.
                async with self.lock_for(ctx.call_id):
                    result = await self.invoke(tool, args, ctx)
            else:
                result = await self.invoke(tool, args, ctx)
        except AppError as error:
            result = {'error': error.code, 'message': error.message}
        except Exception:
            result = {'error': 'tool_failed',
                      'message': 'That action could not be completed. Nothing was changed.'}
        return self.finish(ctx, name, tool, started, result)

    def authorise(self, tool, ctx):
        if tool.risk == VERIFIED and not ctx.verified:
            raise ToolError(403, 'verification_required',
                            'The caller has not been verified, so this information cannot be '
                            'read or changed. Explain that you cannot access existing records '
                            'without verification, and offer to take a message instead.')

    def validate(self, tool, arguments):
        if not isinstance(arguments, dict):
            raise ToolError(422, 'bad_arguments', 'Arguments must be an object.')
        schema = tool.parameters
        allowed = set((schema.get('properties') or {}).keys())
        unknown = set(arguments) - allowed
        if unknown:
            raise ToolError(422, 'unknown_argument',
                            f'Unexpected argument(s): {", ".join(sorted(unknown))}.')
        for required in schema.get('required') or []:
            if required not in arguments or arguments[required] in (None, ''):
                raise ToolError(422, 'missing_argument', f'{required} is required.')
        for key, value in arguments.items():
            expected = (schema['properties'][key] or {}).get('type')
            if expected == 'string' and not isinstance(value, str):
                raise ToolError(422, 'bad_argument', f'{key} must be text.')
            if expected == 'integer' and not isinstance(value, int):
                raise ToolError(422, 'bad_argument', f'{key} must be a whole number.')
        return arguments

    async def invoke(self, tool, args, ctx):
        result = tool.handler(args, ctx)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def finish(self, ctx, name, tool, started, result):
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        ok = not (isinstance(result, dict) and result.get('error'))
        summary = f'{name} {"ok" if ok else result.get("error")} ({elapsed} ms)'
        try:
            # The events table is the audit trail; it already survives restarts.
            self.db.event(ctx.workspace, 'tool.' + name, summary, ctx.call_id)
        except Exception:
            pass
        if isinstance(result, dict):
            result = {**result, 'elapsed_ms': elapsed}
        if ok and tool is not None and tool.advances_to:
            ctx.phase = tool.advances_to
        return result

    # -- the clinic's tools -------------------------------------------
    def register_defaults(self):
        db = self.db

        def hours(args, ctx):
            s = db.settings(ctx.workspace)
            return {'open_hour': s['open_hour'], 'close_hour': s['close_hour'],
                    'timezone': 'Asia/Kolkata',
                    'note': 'Holiday exceptions are not configured in this demo.'}

        def fee(args, ctx):
            return {'fee_inr': db.settings(ctx.workspace)['fee'],
                    'note': 'Demo figure, not a real quote.'}

        def location(args, ctx):
            return {'address': db.settings(ctx.workspace)['address'],
                    'note': 'No map or message has been sent.'}

        def slots(args, ctx):
            found = db.slots(ctx.workspace, str(args['date'])[:10])[:6]
            return {'date': args['date'], 'slots': found,
                    'note': 'Availability is not a reservation. Use hold_slot then confirm_booking.'}

        def hold(args, ctx):
            name = str(args['name']).strip()
            if not 2 <= len(name) <= 80:
                raise ToolError(422, 'name', 'Ask the caller for a name of 2 to 80 characters.')
            starts_at = str(args['starts_at'])
            available = db.slots(ctx.workspace, starts_at[:10])
            slot = next((s for s in available if s['starts_at'] == starts_at), None)
            if not slot:
                raise ToolError(409, 'slot_unavailable',
                                'That time is not available. Offer the caller the current slots.')
            ctx.state['held'] = {'starts_at': starts_at, 'name': name}
            return {'held': True, 'booked': False, 'name': name,
                    'date': starts_at[:10], 'time': slot['label'],
                    'next': 'Read this back to the caller and wait for a clear yes, '
                            'then call confirm_booking.'}

        def confirm(args, ctx):
            held = ctx.state.get('held')
            if not held:
                raise ToolError(409, 'nothing_held', 'Nothing is staged. Call hold_slot first.')
            appointment = db.book(ctx.workspace, ctx.call_id, held['name'], held['starts_at'])
            ctx.state.pop('held', None)
            return {'committed': True, 'appointment': dict(appointment),
                    'note': 'Local calendar only. No SMS, WhatsApp or external calendar '
                            'message has been sent.'}

        def staff_request(args, ctx):
            task = db.task(ctx.workspace, ctx.call_id, str(args['reason'])[:200])
            return {'queued': True, 'id': task['id'],
                    'note': 'A staff review request. Not a transfer, and no call was placed.'}

        def request_code(args, ctx):
            """Identity verification, honestly unavailable.

            Sending a code needs a messaging channel. None is connected, so
            this refuses rather than letting the model tell a caller that a
            message was sent when none was.
            """
            return {'sent': False, 'reason': 'no_messaging_channel_connected',
                    'message': 'No SMS or WhatsApp channel is connected on this system, so the '
                               'caller cannot be verified. Say exactly that, do not claim a code '
                               'was sent, and offer to take a message for the team instead.'}

        def my_appointments(args, ctx):
            rows = [a for a in db.appointments(ctx.workspace)
                    if ctx.patient_ref and a['name'] == ctx.patient_ref]
            return {'appointments': rows}

        def cancel_mine(args, ctx):
            target = str(args['appointment_id'])
            row = next((a for a in db.appointments(ctx.workspace) if a['id'] == target), None)
            if not row:
                raise ToolError(404, 'appointment_missing', 'No such appointment.')
            if not ctx.patient_ref or row['name'] != ctx.patient_ref:
                # Verified as someone, but not as this appointment's owner.
                raise ToolError(403, 'not_your_appointment',
                                'That appointment belongs to someone else.')
            return db.cancel_appointment(ctx.workspace, target)

        register = self.register
        register(Tool('get_business_hours', 'Opening hours. Call before stating any hours.',
                      obj(), hours))
        register(Tool('get_fee', 'The consultation fee. Call before stating any price.',
                      obj(), fee))
        register(Tool('get_location', 'The address. Call before stating any address.',
                      obj(), location))
        register(Tool('get_available_slots',
                      'Real free appointment times for a date. Never guess availability.',
                      {'type': 'object', 'additionalProperties': False,
                       'properties': {'date': {'type': 'string',
                                               'description': 'YYYY-MM-DD in Asia/Kolkata'}},
                       'required': ['date']},
                      slots, advances_to='booking'))
        register(Tool('hold_slot',
                      'Stage a booking for confirmation. This does NOT book. Read the details '
                      'back to the caller afterwards and wait for a clear yes.',
                      {'type': 'object', 'additionalProperties': False,
                       'properties': {'starts_at': {'type': 'string'},
                                      'name': {'type': 'string'}},
                       'required': ['starts_at', 'name']},
                      hold, writes=True, latency=MEDIUM, phases=('booking',)))
        register(Tool('confirm_booking',
                      'Commit the held booking. Only after the caller clearly confirmed the '
                      'details you read back. Rechecks availability and may fail.',
                      obj(), confirm, writes=True, latency=MEDIUM, phases=('booking',),
                      advances_to='reception'))
        register(Tool('create_staff_request',
                      'Queue a request for a human. Use for anything clinical, any emergency, '
                      'any request for a person, and anything you cannot handle. This is not a '
                      'transfer and does not place a call.',
                      {'type': 'object', 'additionalProperties': False,
                       'properties': {'reason': {'type': 'string'}}, 'required': ['reason']},
                      staff_request, writes=True, phases=('*',)))
        register(Tool('request_verification_code',
                      'Start identity verification, required before reading or changing an '
                      'existing appointment.',
                      obj(), request_code, advances_to='records'))
        register(Tool('find_my_appointments',
                      "The verified caller's existing appointments.",
                      obj(), my_appointments, risk=VERIFIED, phases=('records',)))
        register(Tool('cancel_my_appointment',
                      "Cancel one of the verified caller's own appointments.",
                      {'type': 'object', 'additionalProperties': False,
                       'properties': {'appointment_id': {'type': 'string'}},
                       'required': ['appointment_id']},
                      cancel_mine, risk=VERIFIED, writes=True, phases=('records',)))
