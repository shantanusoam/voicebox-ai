# Tool orchestration

The realtime model decides *what* it wants done. `callbox/orchestrator.py`
decides whether that is permitted, validates the arguments, serialises anything
that writes, executes it against the deterministic backend, and records what
happened. The model never reaches a database, an API or an MCP server directly.

```text
        Realtime model
              │  "I want book_appointment(...)"
              ▼
    ┌─────────────────────┐
    │    Orchestrator     │
    │  risk tier          │  is the caller allowed this?
    │  argument schema    │  are these arguments well formed?
    │  write serialising  │  is another mutation already running?
    │  execute            │  against the same code the console uses
    │  audit              │  into the events table
    │  phase routing      │  what is publishable next turn?
    └─────────────────────┘
              │
   SQLite  ·  future: MCP gateway, workflow queue
```

## Risk tiers

A phone caller is not authenticated. Caller ID is spoofable, shared across a
household, and identical for every call placed from the clinic's own reception
phone, so it can never gate access to someone else's record. `SECURITY.md`
already states the rule; this is where it is enforced.

| Tier | Meaning | Tools |
|---|---|---|
| `OPEN` | Discloses nothing about an existing patient | hours, fee, location, availability, hold/confirm a **new** booking, staff request |
| `VERIFIED` | Reads or mutates an existing record | `find_my_appointments`, `cancel_my_appointment` |

Booking a new appointment under a name the caller supplies reveals nothing and
needs no verification. Reading or cancelling an existing one does.

`request_verification_code` is implemented and **refuses**: sending a code needs
a messaging channel and none is connected. It returns `sent: false` with an
instruction not to claim otherwise, so the model tells the caller the truth
rather than inventing a delivered SMS. Connect a provider and the same tool
starts working; nothing else changes.

## Writes are serialised

The Realtime API accepts `parallel_tool_calls` (verified). Two concurrent
*mutations* is exactly the race the `one_active_slot` unique index exists to
catch, so tools declare `writes=True` and take a per-call lock in the order the
model asked for them. Reads may overlap freely. That ordering is what makes
`hold_slot` → `confirm_booking` a guarantee rather than a hope.

## Phase routing, and two ways it went wrong

82 tools were accepted by the API without any error, so over-exposure fails
*silently* as worse tool selection and more context per turn. Each phase
publishes a subset.

Both failures below were found by end-to-end calls, not by tests:

1. **Restriction without advancement.** `hold_slot` and `confirm_booking` were
   published only in the `booking` phase, and nothing ever left `reception`.
   The model accurately told the caller it could not book.
2. **Exclusive phase sets.** With transitions added, asking about existing
   records moved the call into `records`, which published no booking tools, and
   the caller was stranded again.

Phases are therefore **additive**: every phase publishes the entry-point tools,
and a phase adds only what it unlocks. A caller changes subject whenever they
like and the tool list has to survive it.

| Phase | Published | Entered by |
|---|---|---|
| `reception` | 6 entry-point tools | call start, completed booking |
| `booking` | + `hold_slot`, `confirm_booking` | `get_available_slots` |
| `records` | + `find_my_appointments`, `cancel_my_appointment` | `request_verification_code` |

When a phase changes, `realtime.py` sends a `session.update` so the next turn is
planned against the new list.

## Audit

Every execution is written to the `events` table, refusals included, with the
tool name, outcome and elapsed milliseconds. `GET /api/events` reads it back.

## Latency classes

Declared per tool so a caller is never left in silence:

| Class | Budget | Handling |
|---|---|---|
| `FAST` | under ~800 ms | answer inline |
| `MEDIUM` | 1-5 s | say a filler line first; the model does this unprompted |
| `SLOW` | longer | not yet implemented - belongs in a workflow queue, with the result delivered out of band |

## Adding MCP later

A remote MCP tool declared as `{type: 'mcp', server_url: ...}` is called by
OpenAI **directly**, which removes this layer from the path and moves argument
validation, approval and audit outside the trust boundary. To keep the
orchestrator in the middle, register MCP-backed capabilities as ordinary
function tools whose handler speaks MCP downstream. The model still sees
`book_appointment()`; the MCP hop happens behind the policy layer, not in front
of it.
