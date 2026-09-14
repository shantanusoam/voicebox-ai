# Verification evidence - CallBox 0.2.0

Verified in the development environment on 13 September 2026. These results are scoped to the local software; they are not proof of live telephone operation or production readiness.

## Executed

| Check | Result | Evidence |
|---|---|---|
| Python API/domain/security/provider-contract regressions | 119 passed | qa/final-tests.txt |
| Chromium UI workflows, responsive widths and interaction checks | 86 passed, zero page errors | qa/browser-results.json |
| Native Python HTTP/WebSocket loopback | 100 / 100 frames byte-identical; 64,000 bytes | qa/native-loopback.json |
| Browser-triggered loopback through explicit relay | 50 / 50 frames byte-identical | qa/browser-loopback.json |
| Portable firmware queue | C11 host compilation and assertions, including 10,000 wrap cycles | qa/firmware-host-tests.txt |
| Python compilation and JavaScript syntax | Passed; static assets prepared | qa/build.txt |
| Static HTTP routes | Six assets/docs routes return 200 | Native network check |
| Authentication outside demo | Anonymous API rejected; demo login disabled | Native network check |

Tests cover explicit confirmation, slot races, duplicate request binding, paid-audio concurrency, database persistence/restart handling, expired/ended calls, medical/human request boundaries, API authentication, scoped reads, SQL input handling, device revocation, PCM validation, interruption epochs, bounded buffers and provider-error redaction. Recent regressions include non-ASCII invalid tokens, idle lock cleanup and dry-run versus applied retention.

## Browser method and limitations

The managed Chromium in this environment prevents navigation to localhost and file URLs. The successful run used `python scripts/browser_smoke.py --relay`: the exact console HTML/CSS/JS is injected into Chromium, while a Python bridge sends its API requests to a real fresh HTTP server. The bridge also connects a real native WebSocket for the loopback test. UI state and backend outcomes are real; the browser transport is substituted and labelled.

This does NOT verify native browser fetch/WebSocket behavior, TLS, browser cookies/CSP enforcement, microphone permissions, MediaRecorder encoding or audible speech. HTTP tests inspect authentication and security headers separately. A normal developer machine should run `python scripts/browser_smoke.py` without `--relay` to close that gap. The delivered native Python loopback independently tests actual HTTP/WebSocket networking without the browser shim.

Layout checks cover seven views at six widths: 320, 360, 390, 768, 1024 and 1440 pixels. Tables may scroll inside their containers; the document does not overflow horizontally. Keyboard checks include mobile-menu Escape/focus restoration and native modal dismissal. This is not a complete WCAG audit or screen-reader test.

## Not executed

No OpenAI paid requests; all provider tests use fake HTTP responses or an injected fake provider. No ESP-IDF compilation or connected board. No Bluetooth/SCO/HFP test. No actual phone call or carrier/SIP service. No doctor/patient/real calendar data. No Docker build, GitHub Actions remote run, public deployment, load test, penetration test, Firefox/Safari check or physical Android microphone test.

Native loopback timing is localhost-only and excludes radio, telecom, audio codec, speech models and Internet travel. Do not promote its sub-millisecond/millisecond results as phone-agent latency.

## Reproduce

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/build.py
make -C firmware test
python scripts/network_check.py
python -m playwright install chromium
python scripts/browser_smoke.py
```

The clean-archive check delivered alongside the ZIP reruns backend tests, build, C host tests and native networking from an extracted copy using the existing environment dependencies. It is not a fresh Internet package-install test.
