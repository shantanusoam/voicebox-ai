"""Bound SQL, tenant-scoped reads, transactional booking, and hashed credentials.

SQLite is deliberately single-process here. Multi-worker/distributed operation
requires moving locks/admission control to a shared store and migrating the DB.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from .errors import AppError

IST = ZoneInfo('Asia/Kolkata')

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def ident(prefix):
    return prefix + '_' + uuid.uuid4().hex[:18]

def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls(
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id), label TEXT NOT NULL,
 language TEXT NOT NULL, source TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL DEFAULT '{}',
 outcome TEXT NOT NULL DEFAULT 'In progress', sample INTEGER NOT NULL DEFAULT 0,
 device_id TEXT, consent INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS calls_workspace ON calls(workspace,started_at DESC);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
 role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS appointments(
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id), call_id TEXT REFERENCES calls(id) ON DELETE SET NULL,
 name TEXT NOT NULL, starts_at TEXT NOT NULL, duration INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'confirmed', created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_slot ON appointments(workspace,starts_at) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id), call_id TEXT REFERENCES calls(id) ON DELETE SET NULL,
 reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices(
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id), name TEXT NOT NULL,
 kind TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'offline',
 last_seen TEXT, revoked INTEGER NOT NULL DEFAULT 0, frames INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL REFERENCES workspaces(id),
 kind TEXT NOT NULL, summary TEXT NOT NULL, call_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(
 workspace TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, payload_hash TEXT NOT NULL,
 response TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(workspace,scope,key));
'''

class Database:
    def __init__(self, path: Path | str, workspace='clinic-demo', seed=True):
        if str(path) != ':memory:': Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute('INSERT OR IGNORE INTO migrations VALUES(1,?)', (now_iso(),))
        self.create_workspace(workspace)
        self.execute("UPDATE devices SET status='offline'")
        # A process restart cannot silently resume a call or claim a live device.
        self.execute("UPDATE calls SET status='ended', ended_at=?, outcome='Server restarted' WHERE status='active'", (now_iso(),))
        if seed and not self.one('SELECT id FROM calls WHERE workspace=? LIMIT 1', (workspace,)):
            self.seed(workspace)

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.conn.in_transaction
            savepoint = 'sp_' + uuid.uuid4().hex
            self.conn.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                yield
                self.conn.execute('RELEASE SAVEPOINT ' + savepoint if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.conn.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                    self.conn.execute('RELEASE SAVEPOINT ' + savepoint)
                else:
                    self.conn.execute('ROLLBACK')
                raise

    def execute(self, sql, args=()):
        with self.lock: return self.conn.execute(sql, args)
    def one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None
    def all(self, sql, args=()):
        with self.lock: return [dict(x) for x in self.conn.execute(sql, args).fetchall()]
    def close(self):
        with self.lock: self.conn.close()

    def create_workspace(self, workspace):
        settings = dict(name='Willow Clinic', business='clinic', timezone='Asia/Kolkata',
                        open_hour=10, close_hour=19, slot_minutes=30, fee=500,
                        address='Demo address - replace before any pilot', staff_label='Front desk team')
        self.execute('INSERT OR IGNORE INTO workspaces VALUES(?,?)', (workspace, json.dumps(settings)))

    def settings(self, workspace):
        row = self.one('SELECT settings FROM workspaces WHERE id=?', (workspace,))
        if not row: raise AppError(404, 'workspace_missing', 'Workspace not found.')
        return json.loads(row['settings'])

    def set_settings(self, workspace, settings):
        self.execute('UPDATE workspaces SET settings=? WHERE id=?', (json.dumps(settings), workspace))
        self.event(workspace, 'settings.updated', 'Business settings saved')
        return settings

    def event(self, workspace, kind, summary, call_id=None):
        self.execute('INSERT INTO events(workspace,kind,summary,call_id,created_at) VALUES(?,?,?,?,?)',
                     (workspace, kind, summary, call_id, now_iso()))

    def events(self, workspace, after=0):
        return self.all('SELECT * FROM events WHERE workspace=? AND id>? ORDER BY id DESC LIMIT 60', (workspace, after))

    def new_call(self, workspace, label, language='en', source='browser', provider='local', consent=False,
                 device_id=None, sample=False):
        cid = ident('call')
        self.execute('''INSERT INTO calls(id,workspace,label,language,source,provider,status,started_at,state,sample,device_id,consent)
                     VALUES(?,?,?,?,?,?,'active',?,'{}',?,?,?)''',
                     (cid, workspace, label, language, source, provider, now_iso(), int(sample), device_id, int(consent)))
        self.event(workspace, 'call.started', f'{source} test session started', cid)
        return self.call(workspace, cid)

    def call(self, workspace, cid):
        row = self.one('SELECT * FROM calls WHERE id=? AND workspace=?', (cid, workspace))
        if not row: raise AppError(404, 'call_missing', 'Call not found.')
        row['state'] = json.loads(row['state'])
        row['sample'] = bool(row['sample'])
        row['consent'] = bool(row['consent'])
        row['messages'] = self.all('SELECT role,text,created_at FROM messages WHERE call_id=? ORDER BY id', (cid,))
        return row

    def calls(self, workspace, query=''):
        rows = self.all('''SELECT id,label,language,source,provider,status,started_at,ended_at,outcome,sample
            FROM calls WHERE workspace=? AND (label LIKE ? OR outcome LIKE ?) ORDER BY started_at DESC LIMIT 200''',
            (workspace, '%' + query + '%', '%' + query + '%'))
        return rows

    def state(self, workspace, cid, value):
        self.execute('UPDATE calls SET state=? WHERE id=? AND workspace=?', (json.dumps(value), cid, workspace))

    def message(self, cid, role, text):
        self.execute('INSERT INTO messages(call_id,role,text,created_at) VALUES(?,?,?,?)', (cid, role, text, now_iso()))

    def end_call(self, workspace, cid, outcome=None):
        call = self.call(workspace, cid)
        if call['status'] != 'ended':
            outcome = outcome or ('Completed' if call['outcome'] == 'In progress' else call['outcome'])
            self.execute("UPDATE calls SET status='ended',ended_at=?,outcome=? WHERE id=? AND workspace=?", (now_iso(), outcome, cid, workspace))
            self.event(workspace, 'call.ended', outcome, cid)
        return self.call(workspace, cid)

    def cached(self, workspace, scope, key, payload):
        row = self.one('SELECT * FROM idempotency WHERE workspace=? AND scope=? AND key=?', (workspace, scope, key))
        if row:
            if row['payload_hash'] != digest(payload): raise AppError(409, 'idempotency_conflict', 'Request ID reused with different data.')
            return json.loads(row['response'])
        return None

    def cache(self, workspace, scope, key, payload, response):
        self.execute('INSERT INTO idempotency VALUES(?,?,?,?,?,?)',
                     (workspace, scope, key, digest(payload), json.dumps(response), now_iso()))

    def slots(self, workspace, date_str):
        settings = self.settings(workspace)
        try: day = datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError): raise AppError(422, 'invalid_date', 'Use a valid YYYY-MM-DD date.')
        today = datetime.now(IST).date()
        if day < today or day > today + timedelta(days=60):
            raise AppError(422, 'date_range', 'Choose today or a date in the next 60 days.')
        start = datetime(day.year, day.month, day.day, settings['open_hour'], tzinfo=IST)
        end = start.replace(hour=settings['close_hour'])
        duration = settings['slot_minutes']
        occupied = {x['starts_at'] for x in self.all("SELECT starts_at FROM appointments WHERE workspace=? AND status='confirmed'", (workspace,))}
        result = []
        while start + timedelta(minutes=duration) <= end:
            value = start.isoformat(timespec='seconds')
            if start > datetime.now(IST) and value not in occupied:
                result.append(dict(starts_at=value, label=start.strftime('%I:%M %p').lstrip('0'), duration=duration))
            start += timedelta(minutes=duration)
        return result

    def book(self, workspace, cid, name, starts_at):
        with self.transaction():
            self.call(workspace, cid)
            # Re-evaluate availability at commit, not when an LLM suggested it.
            slots = self.slots(workspace, starts_at[:10])
            slot = next((s for s in slots if s['starts_at'] == starts_at), None)
            if not slot: raise AppError(409, 'slot_unavailable', 'This slot is no longer available. Choose another.')
            aid = ident('apt')
            try:
                self.execute('INSERT INTO appointments VALUES(?,?,?,?,?,?,?,?)',
                             (aid, workspace, cid, name, starts_at, slot['duration'], 'confirmed', now_iso()))
            except sqlite3.IntegrityError:
                raise AppError(409, 'slot_unavailable', 'This slot was just booked. Choose another.')
            self.execute('UPDATE calls SET outcome=? WHERE id=? AND workspace=?', ('Appointment booked (local calendar)', cid, workspace))
            self.event(workspace, 'appointment.booked', 'Local calendar booking confirmed', cid)
            return self.one('SELECT * FROM appointments WHERE id=? AND workspace=?', (aid, workspace))

    def appointments(self, workspace):
        return self.all('SELECT * FROM appointments WHERE workspace=? ORDER BY starts_at LIMIT 500', (workspace,))

    def cancel_appointment(self, workspace, aid):
        with self.transaction():
            row = self.one('SELECT * FROM appointments WHERE id=? AND workspace=?', (aid, workspace))
            if not row: raise AppError(404, 'appointment_missing', 'Appointment not found.')
            if row['status'] != 'cancelled':
                self.execute("UPDATE appointments SET status='cancelled' WHERE id=? AND workspace=?", (aid, workspace))
                self.event(workspace, 'appointment.cancelled', 'Operator cancelled a local appointment', row['call_id'])
        return {'status': 'cancelled', 'id': aid}

    def task(self, workspace, cid, reason):
        existing = self.one("SELECT * FROM tasks WHERE workspace=? AND call_id=? AND status='open'", (workspace, cid))
        if existing: return existing
        tid = ident('task')
        self.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)', (tid, workspace, cid, reason, 'open', now_iso()))
        self.execute('UPDATE calls SET outcome=? WHERE id=? AND workspace=?', ('Staff review requested', cid, workspace))
        self.event(workspace, 'task.created', 'Staff review queued - no phone transfer', cid)
        return self.one('SELECT * FROM tasks WHERE id=?', (tid,))

    def tasks(self, workspace):
        return self.all('SELECT * FROM tasks WHERE workspace=? ORDER BY created_at DESC LIMIT 200', (workspace,))

    def resolve_task(self, workspace, tid):
        row = self.one('SELECT * FROM tasks WHERE workspace=? AND id=?', (workspace, tid))
        if not row: raise AppError(404, 'task_missing', 'Task not found.')
        self.execute("UPDATE tasks SET status='resolved' WHERE workspace=? AND id=?", (workspace, tid))
        self.event(workspace, 'task.resolved', 'Operator resolved a staff request', row['call_id'])
        return {'status': 'resolved'}

    def create_device(self, workspace, name, kind):
        did, token = ident('dev'), secrets.token_urlsafe(36)
        self.execute('''INSERT INTO devices(id,workspace,name,kind,token_hash,created_at) VALUES(?,?,?,?,?,?)''',
                     (did, workspace, name, kind, digest(token), now_iso()))
        self.event(workspace, 'device.provisioned', f'{kind} credentials created')
        return {'id': did, 'name': name, 'kind': kind, 'token': token,
                'warning': 'Token shown once. This does not pair or flash hardware.'}

    def devices(self, workspace):
        return self.all('SELECT id,name,kind,status,last_seen,revoked,frames,created_at FROM devices WHERE workspace=? ORDER BY created_at DESC', (workspace,))

    def authenticate_device(self, token, did):
        if not isinstance(token, str) or len(token) > 200 or not isinstance(did, str) or len(did) > 100: return None
        return self.one('SELECT * FROM devices WHERE id=? AND token_hash=? AND revoked=0', (did, digest(token)))

    def touch_device(self, did, online=True, frames=0):
        self.execute('UPDATE devices SET status=?,last_seen=?,frames=frames+? WHERE id=?', ('online' if online else 'offline', now_iso(), frames, did))

    def revoke_device(self, workspace, did):
        row = self.one('SELECT id FROM devices WHERE id=? AND workspace=?', (did, workspace))
        if not row: raise AppError(404, 'device_missing', 'Device not found.')
        self.execute("UPDATE devices SET revoked=1,status='offline' WHERE id=? AND workspace=?", (did, workspace))
        self.event(workspace, 'device.revoked', 'Device credentials revoked')
        return {'id': did, 'revoked': True}

    def login(self, workspace, seconds):
        token = secrets.token_urlsafe(36)
        self.execute('DELETE FROM sessions WHERE expires_at<?', (time.time(),))
        self.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), workspace, time.time() + seconds))
        return token

    def authenticate_session(self, token):
        if not token: return None
        return self.one('SELECT workspace FROM sessions WHERE token_hash=? AND expires_at>?', (digest(token), time.time()))

    def summary(self, workspace):
        calls = self.calls(workspace)
        apts = self.appointments(workspace)
        tasks = self.tasks(workspace)
        devices = self.devices(workspace)
        return dict(calls=len(calls), sample_calls=sum(bool(x['sample']) for x in calls),
                    active=sum(x['status']=='active' for x in calls),
                    booked=sum(x['status']=='confirmed' for x in apts),
                    review=sum(x['status']=='open' for x in tasks),
                    online=sum(x['status']=='online' for x in devices),
                    today=datetime.now(IST).strftime('%A, %d %B %Y'), timezone='Asia/Kolkata')

    def seed(self, workspace):
        examples = [('Aarav - sample','Clinic hours','What time do you open?','This demo clinic opens at 10 AM.'),
                    ('Riya - sample','Location shared','Where is the clinic?','The address in this example is a placeholder.'),
                    ('Kabir - sample','Staff review requested','I would like to speak to someone.','This sample demonstrates a callback request, not a phone transfer.'),
                    ('Meera - sample','Fee enquiry','What is the fee?','The demo consultation fee is INR 500. Replace this before a pilot.')]
        with self.transaction():
            for index, (name, outcome, question, answer) in enumerate(examples):
                call = self.new_call(workspace, name, source='sample', sample=True)
                cid = call['id']
                self.message(cid, 'caller', question)
                self.message(cid, 'assistant', answer)
                self.end_call(workspace, cid, outcome)
                start = datetime.now(timezone.utc) - timedelta(minutes=30 + index * 35)
                self.execute('UPDATE calls SET started_at=?,ended_at=? WHERE id=?',
                             (start.isoformat(), (start+timedelta(seconds=45+index*13)).isoformat(), cid))
                if index == 2: self.task(workspace, cid, 'Sample: caller requested a person')
            self.event(workspace, 'demo.seeded', 'Four synthetic example calls loaded')
