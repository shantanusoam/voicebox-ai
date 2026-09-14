# Running and deployment boundaries

## Local development (verified)

Install the pinned Python requirements and run `python -m callbox`. Serve both website and API from the same process on loopback. The runtime folder is created automatically. Do not run a second process against the same database: process startup marks active sessions ended.

The application requires a persistent filesystem and a long-lived WebSocket-capable server. A static website host alone cannot run it. `build/web/` is a static asset export, not a serverless deployment bundle. `CallBox-Website.html` remains a standalone marketing demo.

## Docker template (provided, not built here)

```bash
export CALLBOX_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build
```

Use the token to sign into http://127.0.0.1:8787/app/. The template disables demo login, binds the published port to loopback, runs a non-root user and keeps SQLite in a named volume. Container build/pull and dependency installation require Internet access. The image and Compose setup were not executed in this environment. Do not publish them to the Internet unchanged.

## Remote development or pilot (not deployed)

Use an appropriate VM/container host supporting long-lived HTTP and WebSockets, persistent storage, TLS, proper secret management and a single worker for this release. Configure exactly the intended public origin and ensure proxy behavior is understood. Never expose a demo-mode service through a tunnel or reverse proxy. Read SECURITY.md before any exposure. Carrier and device connectivity require separate implementation and authorization.

No remote repository was created or modified, no hosting account was accessed, and no CI workflow was run remotely. `.github/workflows/checks.yml` is a supplied verification template. It does not prove a successful GitHub Actions run.
