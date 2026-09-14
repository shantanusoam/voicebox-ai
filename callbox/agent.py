"""A narrow, stateful receptionist with deterministic write permissions.

The local parser is a transparent test fixture, NOT a clinically validated
triage system. Optional model classification never bypasses confirmation or DB
validation. Every medical/unclear case queues a staff task; no live transfers.
"""
import asyncio
from datetime import datetime, timedelta
import re
import time
import weakref
from .db import IST
from .errors import AppError

EMERGENCY = re.compile(r'\b(emergency|chest pain|cannot breathe|can.t breathe|unconscious|suicid\w*|overdose|saans nahi|behosh)\b', re.I)
MEDICAL = re.compile(r'\b(medic\w*|prescri\w*|dosage|dose|diagnos\w*|symptom\w*|pain|fever|bleed\w*|report\w*|tablet\w*|dawai|bukhar|dard|illness|pregnan\w*)\b', re.I)
HUMAN = re.compile(r'\b(human|person|staff|receptionist|callback|call back|doctor se|speak to|talk to)\b', re.I)
YES = {'yes','yes please','confirm','confirmed','haan','ha','ji','book it','confirm booking'}
NO = {'no','nahi','cancel','stop','never mind','nevermind'}


def infer_date(text):
    today = datetime.now(IST).date()
    value = re.search(r'\b\d{4}-\d{2}-\d{2}\b', text)
    if value: return value.group()
    if re.search(r'\b(day after tomorrow|parso)\b', text, re.I): return str(today + timedelta(days=2))
    if re.search(r'\b(tomorrow|kal)\b', text, re.I): return str(today + timedelta(days=1))
    if re.search(r'\b(today|aaj)\b', text, re.I): return str(today)
    return None


def local_intent(text):
    if EMERGENCY.search(text): return 'emergency'
    if MEDICAL.search(text): return 'medical'
    if HUMAN.search(text): return 'human'
    if re.search(r'\b(reschedul\w*|cancel appointment|change appointment)\b', text, re.I): return 'human'
    if re.search(r'\b(book|appointment|booking|slot|appointment chahiye)\b', text, re.I): return 'book'
    if re.search(r'\b(hours|open|close|timing|kab khul|available days)\b', text, re.I): return 'hours'
    if re.search(r'\b(fee|fees|price|cost|charges|kitne)\b', text, re.I): return 'fee'
    if re.search(r'\b(address|location|where|kahan)\b', text, re.I): return 'location'
    return 'unknown'


class Agent:
    def __init__(self, db, config, provider):
        self.db, self.config, self.provider = db, config, provider
        self.locks = weakref.WeakValueDictionary()

    def lock(self, cid):
        # Weak values release idle locks; waiting coroutines keep a strong reference.
        return self.locks.setdefault(cid, asyncio.Lock())

    def greeting(self, call, settings):
        if call['language'] == 'hinglish':
            return f"Namaste, {settings['name']} ka AI assistant. Yeh test session hai. Main appointment aur clinic information mein help kar sakta hoon. Aapko kya chahiye?"
        return f"Hello, I'm the AI assistant for {settings['name']}. This is a test session. I can help with appointments, opening hours, or a message for the team. How can I help?"

    def start(self, workspace, **values):
        active = self.db.one("SELECT COUNT(*) n FROM calls WHERE workspace=? AND status='active'", (workspace,))['n']
        if active >= self.config.max_active_calls:
            raise AppError(429, 'capacity', 'Active session limit reached. End an existing test session first.')
        if values.get('provider') == 'openai':
            if not self.config.api_key: raise AppError(503, 'provider_not_configured', 'Configure OPENAI_API_KEY before selecting paid speech.')
            if not values.get('consent'): raise AppError(422, 'consent_required', 'Explicit consent is required before sending test content to a provider.')
        with self.db.transaction():
            call = self.db.new_call(workspace, **values)
            self.db.message(call['id'], 'assistant', self.greeting(call, self.db.settings(workspace)))
        return self.db.call(workspace, call['id'])

    async def turn(self, workspace, cid, text, request_id):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise AppError(422, 'text_length', 'Provide between 1 and 2000 characters.')
        text = text.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id or ''):
            raise AppError(422, 'request_id', 'A unique request ID of 8-100 safe characters is required.')
        async with self.lock(cid):
            start = time.perf_counter()
            cached = self.db.cached(workspace, cid, request_id, text)
            if cached: return {**cached, 'replayed': True}
            call = self.db.call(workspace, cid)
            if call['status'] != 'active': raise AppError(409, 'call_ended', 'This test session has ended. Start a new one.')
            age = datetime.now().astimezone() - datetime.fromisoformat(call['started_at'])
            if age.total_seconds() > self.config.max_call_seconds:
                self.db.end_call(workspace, cid, 'Maximum session duration reached')
                raise AppError(409, 'call_expired', 'Session time limit reached. Start a new test.')
            if len(call['messages']) >= 161:
                raise AppError(429, 'turn_limit', 'This test has reached its 80-turn limit. End it and start another.')
            intent = local_intent(text)
            date_hint = infer_date(text)
            # Do not let an LLM reinterpret confirmation, names, or selected slots.
            if call['provider'] == 'openai' and not call['state'].get('step') and intent == 'unknown':
                result = await self.provider.classify(text, str(datetime.now(IST).date()))
                intent, date_hint = result['intent'], result['date'] or date_hint
            # No await inside transaction. Cancellation cannot half-commit a booking.
            with self.db.transaction():
                self.db.message(cid, 'caller', text)
                reply, actions = self.apply(workspace, call, text, intent, date_hint)
                self.db.message(cid, 'assistant', reply)
                response = {'reply':reply, 'actions':actions, 'intent':intent,
                            'elapsed_ms':round((time.perf_counter()-start)*1000, 1),
                            'call':self.db.call(workspace, cid), 'replayed':False}
                self.db.cache(workspace, cid, request_id, text, response)
                self.db.event(workspace, 'turn.completed', f'{intent} turn processed', cid)
            return response

    def apply(self, workspace, call, text, intent, date_hint):
        cid, state = call['id'], call['state'].copy()
        settings = self.db.settings(workspace)
        hinglish = call['language'] == 'hinglish'
        actions = []
        def save(): self.db.state(workspace, cid, state)
        def pick_date(date):
            nonlocal state
            try: slots = self.db.slots(workspace, date)
            except AppError as error:
                state = {'step':'date'}; save(); return error.message
            if not slots:
                state = {'step':'date'}; save()
                return 'There are no available slots on that date. Please give another date in YYYY-MM-DD format.'
            state = {'step':'slot','date':date,'slots':slots[:6]}; save()
            actions.append({'tool':'get_available_slots','status':'ok','date':date,'slots':slots[:6]})
            options = ', '.join(f"{i+1}: {s['label']}" for i,s in enumerate(slots[:6]))
            prefix = f"{date} ko available slots" if hinglish else f'Available on {date}'
            return f'{prefix} (India time): {options}. Reply with the option number.'

        if intent in {'emergency','medical','human'}:
            state = {}; save()
            task = self.db.task(workspace, cid, {'emergency':'Possible emergency language - requires approved safety handling',
                                'medical':'Clinical request - clinician review required', 'human':'Caller requested staff assistance'}[intent])
            actions.append({'tool':'create_staff_request','status':'ok','id':task['id']})
            if intent == 'emergency':
                return ('I cannot assess emergencies. Please contact local emergency services now; do not wait for this assistant or a callback. This test does not place an emergency call. I have flagged the conversation for staff review.', actions)
            if intent == 'medical':
                return ('I cannot diagnose, interpret reports, or change medication. I have created a staff-review request. This is not a live transfer and does not guarantee a response time.', actions)
            return ('Maine team ke liye callback request banayi hai. Yeh live phone transfer nahi hai.' if hinglish else
                    'I have created a callback request for the team. This is not a live transfer; no outgoing call has been placed.', actions)

        if text.lower().strip(' .!') in NO:
            state = {}; save()
            return ('The pending request is cleared. Existing appointments have not been cancelled. What would you like to do next?', actions)

        if state.get('step') == 'date':
            if not date_hint: return ('Please give a date, for example tomorrow or YYYY-MM-DD. Dates are interpreted in India time.', actions)
            return pick_date(date_hint), actions

        if state.get('step') == 'slot':
            match = re.fullmatch(r'(?:option\s*)?([1-6])', text, re.I)
            selected = int(match.group(1))-1 if match else -1
            if selected < 0 or selected >= len(state['slots']):
                return ('Please choose a displayed option number, or say cancel to start again.', actions)
            state['chosen'] = state['slots'][selected]
            state['step'] = 'name'; save()
            return ('Booking ke liye naam bata dijiye. Test mein fictional naam use karein.' if hinglish else
                    'What name should I put on the booking? Please use a fictional name for this test.', actions)

        if state.get('step') == 'name':
            name = re.sub(r'^(my name is|i am|mera naam|name is)\s+', '', text, flags=re.I).strip(' .')
            if not 2 <= len(name) <= 80 or re.search(r'[<>\d\r\n]', name):
                return ('Please give a name of 2-80 characters, with no numbers or markup.', actions)
            state['name'] = name; state['step'] = 'confirm'; save()
            return (f"Please confirm: {name}, {state['date']} at {state['chosen']['label']} India time. Say confirm to book in this local test calendar, or cancel.", actions)

        if state.get('step') == 'confirm':
            if text.lower().strip(' .!') not in YES:
                return ('Say confirm to make this exact local booking, or cancel. Nothing has been booked yet.', actions)
            try:
                apt = self.db.book(workspace, cid, state['name'], state['chosen']['starts_at'])
            except AppError as error:
                if error.code != 'slot_unavailable': raise
                date = state['date']; reply = pick_date(date)
                return ('That slot was just taken. ' + reply, actions)
            reply = f"Confirmed in the local calendar: {state['name']} on {state['date']} at {state['chosen']['label']} India time. No WhatsApp, SMS, or external-calendar message has been sent."
            state = {}; save()
            actions.append({'tool':'book_appointment','status':'confirmed','appointment':apt})
            return reply, actions

        if intent == 'book':
            if date_hint: return pick_date(date_hint), actions
            state = {'step':'date'}; save()
            return ('Kis date ki appointment chahiye? Tomorrow ya YYYY-MM-DD bata dijiye.' if hinglish else
                    'Which date would you like? Say tomorrow or give a date as YYYY-MM-DD.', actions)
        if intent == 'hours':
            return (f"{settings['name']} is open from {settings['open_hour']:02d}:00 to {settings['close_hour']:02d}:00 India time in this demo schedule. Holiday exceptions are not configured.", [{'tool':'get_business_hours','status':'ok'}])
        if intent == 'fee':
            return (f"The configured demo fee is INR {settings['fee']}. This is an example, not a real clinic quote.", [{'tool':'get_fee','status':'ok'}])
        if intent == 'location':
            return (f"Configured address: {settings['address']}. No map or message has been sent.", [{'tool':'get_location','status':'ok'}])
        task = self.db.task(workspace, cid, 'Unrecognized request - needs human review')
        return ('I am not sure how to handle that safely. I have queued it for staff review. You can also ask for an appointment, opening hours, fees, or location.', [{'tool':'create_staff_request','status':'ok','id':task['id']}])
