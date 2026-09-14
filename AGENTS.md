# Instructions for coding agents working in this repository

Read README.md, docs/ARCHITECTURE.md and docs/SECURITY.md first. Run the baseline tests before modifying behavior. Never claim a physical device, carrier, provider, browser or deployment was tested without a corresponding real execution result.

Use `python -m pytest -q`, `python scripts/build.py`, `make -C firmware test`, and the browser script when changing their respective surfaces. Paid API calls, real calendars, customer data, public deployment, hardware flashing and outbound calls require explicit operator authorization. Keep .env, .runtime, SQLite files and tokens out of commits and release archives.

Preserve deterministic confirmation, transactional slot rechecks, request-id/payload binding, bounded queues, per-call isolation and truthful integration status. Do not convert an unimplemented integration into an enabled UI status merely to make a demo look complete. Mock tests and relay-browser tests must keep their labels. Every new provider/transport needs contract tests, failure tests and a documented live-validation gate.
