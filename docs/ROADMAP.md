# Product and execution plan

## Release delivered: a software lab

The product hypothesis is a small business with repetitive administrative requests and a human available for exceptions. The platform now proves local tools and a network audio protocol. It does not yet prove the key hardware proposition: answering the existing SIM number via an HFP bridge.

| Workstream | This release | Exit gate for next stage |
|---|---|---|
| Product / operator experience | Website, images, console and real workflow UI | Observe another operator completing the local booking and recovery tasks without coaching |
| Application tools | Local bookings, staff requests, transcripts, device management | Add approved business rules, external identity/calendar integration and reconciliation tests |
| Voice gateway | Real authenticated PCM WebSocket with simulator | Run latency/backpressure/failure trials beyond localhost and validate device playback clearing |
| Paid speech | Adapter plus HTTP contract tests | Complete opt-in live API smoke test; assess Hindi/Hinglish, total cost and quality |
| Hardware | Protocol and portable queue, no HFP driver | Prove bidirectional phone audio plus deterministic human takeover on a documented device matrix |
| Pilot | Not run | Security/privacy/carrier review, clinic-approved boundaries, reachable staff and rollback rehearsals |

## Suggested implementation order

1. Run this release and demonstrate a real local booking and its JSON record.
2. Use the Python simulator as the transport reference; verify exact payloads and recovery.
3. Prove the target phone's HFP audio path without AI. Avoid custom PCB/enclosure spending before this gate.
4. Run a paid voice smoke test independently from hardware, then connect the two proven pieces.
5. Integrate a sandbox business calendar using scoped OAuth, idempotent writes and conflict/retry reconciliation. Do not use an unrelated personal calendar as a test fixture.
6. Add approved human transfer/fallback per chosen transport. A callback task alone is not sufficient.
7. Run a supervised, consented pilot with synthetic cases before real customer/patient data.

## Focus and non-goals

The current system handles one workspace and one simple daily schedule. Do not expand into a large CRM, automated clinical triage, mass outbound dialing or multi-tenant SaaS until the core phone path is reliable. A hardware route, cloud-provider route and SIP route may share tools but have different number eligibility, call transfer, quality, scaling and commercial constraints.

## Evaluation set

Measure an end-to-end successful booking, a racing slot conflict, duplicate confirmation, unclear speech, mixed-language date, changed mind, API timeout, phone hangup during commit, human request, staff unavailable, revoked device, network loss and local disable. Preserve ground truth and show what really committed. Track dropped frames and actual provider invoices; do not claim a replacement percentage or fixed cost saving from localhost tests.

## Commercial assumptions

No prices in this release are carrier quotations. The preserved website cost planner is editable assumptions only. Hardware eliminates neither mobile service obligations nor engineering/maintenance costs. A future pilot may support a one-time gateway price plus software/service usage, but profitability requires measured usage, replacement/support costs, provider charges, and business willingness to pay. No revenue promise is made.
