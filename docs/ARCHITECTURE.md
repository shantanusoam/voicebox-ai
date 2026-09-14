# Architecture and implementation boundaries

## One platform, separate transports

```text
 Browser text --HTTP--\
 Browser mic  --HTTP---+--> FastAPI --> per-call agent --> scoped SQLite tools
 Device lab   --WS--- /          |             |              |
                                |             |         bookings / requests
                                |       optional provider
                                |       STT / intent / TTS
                                |
                           PCM echo / output

 Phone + ESP32 HFP ---- NOT IMPLEMENTED ----> device protocol
 Carrier + PBX ------- NOT IMPLEMENTED ----> future adapter
```

`callbox/main.py` owns HTTP authentication, validation, UI serving and lifecycle. `db.py` owns authoritative records and transactions. `agent.py` owns a small conversational state machine. `gateway.py` owns authenticated device connection state, epochs, input buffers and output cancellation. `providers.py` isolates external APIs. No browser or model directly writes SQL.

### Booking state

`idle -> date -> displayed slot -> fictional name -> exact confirmation -> transactional commit -> idle`

Availability is not a reservation. The commit rechecks availability, opening hours, time range and the unique confirmed-slot constraint. When a competing call takes a slot, the caller receives newly available choices rather than a false confirmation. Request IDs bind to a payload hash and cache the authoritative response. Reusing an ID with different content is an error.

Only the first six available slots are offered in the current conversation UI. The full local schedule is visible in the calendar. Date interpretation is limited to ISO dates, today/tomorrow and a few Hinglish phrases; `kal` is interpreted as tomorrow in this lab. Holiday calendars, multiple doctors, buffers, natural-language time preferences and external calendar synchronization are not implemented.

### Persistence

SQLite schema version 1 contains workspaces, sessions, devices, calls, messages, appointments, tasks, events and idempotency results. Foreign keys and WAL are enabled. Local transactions and a unique index protect bookings. Records are scoped to the configured workspace. This single-workspace release is not proof of secure commercial multi-tenancy.

### Audio and paid work

Browser microphone input is one bounded recording per HTTP request. WebSocket input is fixed PCM frames committed as a turn. The gateway sends generated WAV speech as normalized PCM frames, at nominal 20 ms spacing. Resampling uses a laboratory linear interpolator, not a production DSP implementation. There is no acoustic echo cancellation, codec negotiation, voice activity detection, automatic turn endpointing or tested full-duplex speech.

Paid audio requests are serialized per session. A cached retry returns the stored text/actions without generating speech again. The cache is written after the authoritative turn, before TTS. A request cancelled while upstream transcription is in flight may still incur provider charges; the local application cannot reverse upstream billing. A process crash between an external API response and local cache persistence can also require reconciliation.

### Cancellation and recovery

Interruption increments the epoch, clears captured input and cancels current output work. Devices must discard stale epochs and clear playback on their side. Completed booking commits survive interruption. A dropped gateway connection ends its active test call; it does not recover a physical handset or place a fallback call. Restart recovery marks active tests ended and devices offline. UI history can resume an active browser call or end it explicitly.

### Operational envelope

One process, one workspace, loopback-only default. Twelve simultaneous active test sessions, 30-minute maximum call/connection age, 80 conversational turns, 45-second gateway idle timeout, 15-second gateway input buffer, 4 MB browser-upload cap, 64 KB JSON-body cap, and gateway message-size/rate checks. These are lab bounds, not benchmarked production capacity. There is no automatic idle-browser-session reaper: end an abandoned session from its history record.

Do not put multiple Uvicorn workers behind this SQLite state model and claim distributed consistency: device ownership, per-call locks and rate limits are process-local. A scale-out version needs a deliberate ownership, persistence, media routing and admission-control design.
