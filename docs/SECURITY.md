# Development security and privacy

## Trust boundary

This is a trusted local laboratory, not a production security review or healthcare certification. Use synthetic callers and names. The default service binds to 127.0.0.1. Demo login is permitted only for loopback connections when CALLBOX_DEMO=1. A local reverse proxy can itself appear to be loopback: NEVER publish a demo-mode server behind a proxy or tunnel.

## Implemented controls

- Random administrator token created locally, login sessions with hashed tokens, HttpOnly/SameSite=Strict cookies, configurable Secure flag.
- Server-only model key. Random, hashed, revocable device tokens. No secrets in WS URLs.
- API record scoping, bound SQL parameters, transactional writes and booking confirmation.
- HTML escaping of user values. CSP on every response and other HTTP security headers. Cross-origin write rejection, Host restrictions and first-message device authentication.
- Host allowlisting, origin checks and rate limiting are enforced by plain ASGI middleware (`callbox/limits.py`) so they cover the **WebSocket** scope as well as HTTP. Before 0.2.1 these lived in an `@app.middleware('http')` handler, which Starlette never invokes for websocket connections: a DNS-rebinding handshake reached device authentication while the same Host was rejected over HTTP, and gateway handshakes were not rate limited at all.
- `.runtime/` is created 0700 and the admin token re-chmodded 0600 on every start. It previously inherited the umask, commonly leaving stored transcripts world-readable.
- Near-silent audio turns are refused locally before any paid provider request, because a chat-model transcriber confabulates words rather than reporting silence. See docs/PROVIDERS.md.
- Finite request/audio buffers, per-session locks, limits, provider timeouts, redacted provider error messages, idempotent request outcomes.
- Explicit paid-mode consent. No application disk storage of raw audio or generated speech.

## Known limitations / required work

The default database is not encrypted at rest. The administrator role has broad access. There are no separate staff roles, OAuth/OIDC, MFA, invitations, tenant billing, real patient identity verification, immutable audit log, managed secret vault, multi-process admission control or formal abuse detection. Plain WS is accepted locally; TLS termination and network policy are deployment tasks. Public Host/origin configuration does not make the system production-ready.

Critical: a phone number is NOT patient authentication. Do not disclose reports or clinical history from caller ID. The sample keyword flags are NOT clinically validated triage. A human-review task is not a guaranteed callback, live transfer or emergency response. A clinician must approve real-world safety protocols; applicable telecom/privacy/device obligations and carrier terms require review for the deployment jurisdiction.

Native browser networking, cookies/CSP enforcement, microphone behavior and provider audio quality were not verified in the restricted browser environment. API tests inspect headers and sessions; browser UI tests used an explicit transport relay. See QA.md.

## Storage and retention

`.runtime/callbox.db` contains conversation text, names, bookings, tasks and cached turn results. Restrict the folder and backups. Exported JSON contains transcript information. Device token values appear once in the console and are removed from the DOM when the dialog closes; clipboard copies remain the operator's responsibility.

```bash
python scripts/prune.py --days 30          # reports eligible ended calls
python scripts/prune.py --days 30 --apply  # deletes eligible calls/transcripts/cache and old events
```

Pruning does not delete appointment names or staff-task information. It is not a complete data-subject deletion workflow. Backups, database WAL files, application exports and upstream provider records need their own retention/deletion policies. SQLite deletion is not a secure erase of disk blocks. For a complete lab reset, stop the service, back up what you need and remove the entire .runtime directory knowingly; this deletes local records and generated tokens.

## Before any network exposure

Use a separate machine/network and synthetic data. Disable demo mode; configure a strong administrator token, an exact CALLBOX_PUBLIC_ORIGIN, HTTPS/WSS, Secure cookies, firewall, trusted reverse-proxy behavior, protected persistent storage and secret injection. Replace the single-admin lab model and test session/cross-origin behavior in real browsers. Keep one process unless gateway ownership and shared state are redesigned. Establish alarms, backups, restore tests and incident handling. Then commission a security/operational review rather than treating this checklist as certification.

No hidden telemetry or automatic phone dialing is included. The optional browser speech service and external AI provider may process data outside this application when explicitly used.
