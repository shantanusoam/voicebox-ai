# Verification evidence

Run `python scripts/verify.py`. It writes `qa/verify-report.json` with a
machine-readable record of every gate, and `evals/out/scorecard.{json,md}` with
the behavioural results. This document describes what those gates do and do
not establish; the numbers live in the generated files, not here, so they
cannot drift from reality.

## What runs

| Gate | Layer | Checks |
|---|---|---|
| `env` | config | Config loads, admin token strength, `.runtime` permissions, no provider key tracked in git |
| `static` | source | Python compiles, JS syntax, static assets build, `docs/openapi.json` matches the code |
| `unit` | domain + API | Full pytest suite, including the hardening regressions from the 0.2.0 review |
| `contract` | provider | Both adapters against `httpx.MockTransport`; no network egress, no paid calls |
| `transport` | gateway | Fresh server on an ephemeral port, six static routes, CSP present, anonymous API rejected, foreign Host rejected, then the device simulator over real HTTP/WebSocket |
| `firmware` | device | `make -C firmware test`: C11 `-Wall -Wextra -Werror -pedantic`, including 10 000 wrap cycles |
| `hardened` | deployment | Second server with `CALLBOX_DEMO=0`: anonymous rejected, demo login refused, cookie flags, token never echoed |
| `evals` | behaviour | The thirteen cases in [EVALS.md](EVALS.md), scored against database ground truth |
| `browser` | UI | Playwright console smoke test, opt-in via `--with-browser` |

The pipeline makes **zero paid provider requests**. `evals/live_smoke.py` is the
only thing that spends money, is never invoked by the pipeline or CI, and is
budget-capped.

## Browser method and its limitation

The managed Chromium in the original build environment blocked navigation to
localhost and file URLs, so the delivered browser evidence used
`python scripts/browser_smoke.py --relay`: the exact console HTML/CSS/JS is
injected into Chromium while a Python bridge carries its API requests to a real
HTTP server. UI state and backend outcomes are real; the browser transport is
substituted, and that substitution is labelled wherever the result appears.

That mode does **not** verify native browser fetch/WebSocket behavior, TLS,
cookie or CSP enforcement by the browser, microphone permissions, MediaRecorder
encoding, or audible speech. On a normal developer machine run
`python scripts/verify.py --with-browser`, which uses native localhost
networking and closes that gap. The `transport` gate independently exercises
real HTTP and WebSocket networking without any browser shim.

## Not executed by the pipeline

No ESP-IDF compilation or connected board. No Bluetooth, SCO or HFP test. No
telephone call, carrier or SIP service. No doctor, patient or real calendar
data. No Docker build, public deployment, load test or penetration test. No
Firefox or Safari check. No physical microphone test. No live provider request
unless you run `evals/live_smoke.py` deliberately.

Loopback timing is localhost-only and excludes radio, telecom, audio codec,
speech models and Internet travel. Do not present sub-millisecond loopback
figures as phone-agent latency.

Layout and accessibility checks cover seven views at 320, 360, 390, 768, 1024
and 1440 pixels, plus mobile-menu Escape/focus restoration and native modal
dismissal. This is not a complete WCAG audit or a screen-reader test.

## Reproduce

```bash
python -m pip install -r requirements-dev.txt
python scripts/verify.py
python scripts/verify.py --with-browser   # needs: python -m playwright install chromium
```

A clean-archive check can rerun the same pipeline from an extracted copy of the
release zip using already-installed dependencies. That is not a fresh
Internet package-install test.
