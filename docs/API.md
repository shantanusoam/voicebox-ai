# API quick reference

Run the server and open `/docs`. The included `docs/openapi.json` is generated from this release. HTTP endpoints require the administrator Bearer token or a valid local login cookie except bootstrap, login and health. Device WebSockets authenticate separately with their own first-message token.

| Endpoint | Purpose |
|---|---|
| GET /healthz | Minimal liveness/version; not a telephony readiness claim |
| GET /api/bootstrap | Demo availability, API version |
| POST /api/auth/demo | Loopback-only demo session when enabled |
| POST /api/auth/login | Administrator-token session |
| POST /api/auth/logout | Revoke current cookie session |
| GET /api/workspace, /api/summary | Actual configuration and local counts |
| GET, POST /api/calls | List or create a synthetic test session |
| GET /api/calls/{id} | Read transcript/state |
| POST /api/calls/{id}/turn | Text + unique request_id; deterministic tools |
| POST /api/calls/{id}/audio | Consented audio body; request_id query; optional paid processing |
| POST /api/calls/{id}/end | Finish local session |
| GET /api/calls/{id}/export | Transcript JSON |
| GET /api/availability?date=YYYY-MM-DD | Local schedule, India time |
| GET /api/appointments | Local booking rows |
| POST /api/appointments/{id}/cancel | Explicit operator cancellation with confirm:true |
| GET /api/tasks | Staff-review queue |
| POST /api/tasks/{id}/resolve | Explicit operator resolution |
| GET, POST /api/devices | List/provision identity; new token shown once |
| POST /api/devices/{id}/revoke | Revoke token and close live gateway |
| GET /api/events | Recent bounded activity records |
| PUT /api/settings | Validated example-business configuration |
| WS /ws/device | Real lab media gateway; see DEVICE-PROTOCOL.md |

Structured application errors have `error.code` and `error.message`. Pydantic input-validation failures have a `detail` array. The client handles both. Queries are bounded/scoped for the lab, but cursor pagination and large-history administration are not implemented.

Bookings and staff requests are created only through the constrained agent path. No arbitrary SQL, shell command, external HTTP-tool URL or calendar credential can be supplied by a model. This developer API has no clinical-record endpoints.
