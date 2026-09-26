# Device serial logs (evidence)

Raw UART captures from the S25 + ESP32 rig. No credentials, phone numbers or
keys appear in these files (scanned before publishing); voice recordings are
never published.

- `session-2026-09-26-agent-live-audio.log` — the successful agent-mode
  session after the streaming-WAV fix: CALL_STARTED -> speech detected ->
  COMMIT -> TURN_PROCESSING -> TURN_RESULT, with STATS/SCO health lines.
  End-to-end AI voice was audible on the phone in this session (~10%
  intelligible; see DEBUG-LOG.md for the downlink coexistence analysis).
- `session-2026-09-26-debug-sequence-errors.log` — the debugging session that
  exposed the binary-batch/gateway mismatch (suppressed closes) and sequence
  gaps; kept as evidence for DEBUG-LOG.md blockers 6-7.
