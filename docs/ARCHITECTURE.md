# Architecture and implementation boundaries

## One business agent, multiple transports and voice runtimes

CallBox separates **transport**, **voice runtime** and **business authority**.
Changing a carrier or speech provider must not change booking rules, tenant
routing or tool permissions.

```text
 Browser mic ---------------------------\
                                         \
 Softphone -> Asterisk -> AudioSocket ----+--> 24 kHz PCM media contract
                                           |          |
                                           |          v
                                           |    VoiceRuntime selector
                                           |      |            |
                                           |      |            |
                                           |   OpenAI       Pipeline
                                           |   Realtime     STT -> LLM -> TTS
                                           |      |            |
                                           +------+------------+
                                                  |
                                                  v
                                             Orchestrator
                                      policy / schema / locks / audit
                                                  |
                                   +--------------+--------------+
                                   |                             |
                              deterministic DB            future MCP/API
                                   |
                              bookings / tasks
```

The local Asterisk path is implemented and can answer a SIP call from a
softphone. It is still **not PSTN service**: there is no carrier, DID or licensed
SIP trunk in this repository. See `telephony/README.md`.

## Voice runtime boundary

`callbox/realtime.py` is the runtime entry point. It selects one of two paths:

- `CALLBOX_VOICE_RUNTIME=openai`: native OpenAI Realtime. OpenAI handles speech,
  VAD/turn-taking and audio generation; CallBox handles tools and data.
- `CALLBOX_VOICE_RUNTIME=pipeline`: local RMS VAD plus replaceable STT,
  tool-calling LLM and TTS components from `callbox/component_providers.py`.

Both paths publish the same orchestrator tools and produce/consume the same PCM
transport events. The browser and SIP bridge therefore do not need provider
specific business logic.

The pipeline is currently sequential STT -> LLM -> TTS. Its input reader stays
active so caller speech can clear queued playback and suppress stale output, but
an upstream HTTP request already sent cannot be cancelled or un-billed. It is a
cost experiment, not yet a production full-duplex claim.

## Tool authority

`callbox/orchestrator.py` is the trust boundary between a model and business
data. A model can request an action; it cannot perform one directly.

```text
Model: book_appointment(...)
            |
            v
      Orchestrator
      - risk tier
      - argument schema
      - write serialization
      - phase routing
      - execution
      - audit event
            |
            v
      deterministic handler
```

Open tools disclose no existing-patient data. Verified tools read or mutate an
existing record and require confirmed identity. Caller ID is never treated as
identity. Writes are serialized per call so `hold_slot -> confirm_booking`
remains ordered even if a model asks for parallel tool calls.

Tool phases are additive. A phase exposes entry-point tools plus the small set
unlocked by the current task; it never strands a caller who changes subject.

For MCP, keep the same boundary: expose an ordinary function tool to the model,
validate/authorize it here, then let the handler speak MCP downstream. Direct
model-to-MCP mutation would move approval and audit outside this trust boundary.

## Multi-tenancy and numbers

A tenant is a workspace. `tenant_numbers` maps an authorized number to exactly
one tenant and stores provider, provider number id, inbound/outbound flags,
caller-ID verification and KYC state.

```text
+918000001001 -> aarogya-clinic
+918000001002 -> sunrise-dental
```

Inbound carrier number formats are normalized before lookup. Unknown or
inactive numbers are refused rather than defaulted to another tenant. Each
tenant has its own language, voice, greeting and persona.

The SIP AudioSocket payload has no dialled number, so the Asterisk dialplan
registers `uuid -> called/caller` out-of-band before the media socket connects.
The bridge correlates the media leg by UUID.

Outbound caller ID must be authorized by the carrier/provider. The local number
service refuses outbound use unless the number is explicitly enabled and marked
verified. Caller-ID name/CNAM is separate and should not be assumed for India.

## Audio contracts

The browser/realtime internal media contract is mono PCM16 at 24 kHz. The
Asterisk lab receives 8 kHz narrowband signed-linear audio and converts it at
the transport boundary. Upsampling satisfies a 24 kHz API contract but does not
restore frequency information lost by a telephone channel.

Names, phone digits and dates must therefore be read back before authoritative
writes. DTMF is captured by the SIP bridge as a reliable fallback for digits.

The low-cost pipeline's local endpointer uses configurable RMS energy and a
short pre-roll. This is deliberately simple and must be benchmarked on actual
browser microphones and telephone noise before production.

## Persistence and consistency

SQLite is authoritative for this development lab. Foreign keys and WAL are
enabled; booking commit rechecks availability and the unique slot constraint.
Idempotency records protect retries.

Process-local state still includes call locks, rate/admission state and media
ownership. Running multiple Uvicorn workers does **not** make this distributed.
Production scale-out requires a deliberate move such as:

```text
voice workers
    |
    +---- Redis: call ownership, distributed locks, pub/sub, queues
    |
    `---- Postgres: tenants, numbers, calls, bookings, audit, usage
```

That migration is a production milestone, not something this SQLite lab claims
to have already solved.

## Cancellation and recovery

Barge-in clears queued playback. In the native runtime the upstream response is
also cancelled; in the component pipeline stale output is suppressed but an
HTTP request already accepted upstream may still finish and be billed.
Completed deterministic writes are never rolled back merely because the caller
interrupts later.

A dropped gateway connection ends its active test call. Restart recovery marks
active tests ended and devices offline. There is no physical handset recovery,
carrier failover or public deployment guarantee yet.

## Current operational envelope

The repository remains a development laboratory:

- one process and SQLite by default;
- local SIP transport proof, no PSTN carrier;
- no production RBAC or isolation certification;
- no production monitoring/billing;
- no live WhatsApp/SMS or Google Calendar integration;
- no medical decision-making;
- provider credentials stay server-side and paid tests require deliberate
  configuration/consent.

See `docs/VOICE-RUNTIME.md`, `docs/ORCHESTRATION.md`, `docs/TENANCY.md` and
`telephony/README.md` for the boundaries of each layer.
