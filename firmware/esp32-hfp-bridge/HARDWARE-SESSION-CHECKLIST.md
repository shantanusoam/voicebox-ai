# Hardware session checklist — prepared 2026-09-18

Everything in "What changed this session" was set up *before* the ESP32 was
connected. "Hardware session log" below is what actually happened once it
was. House rule from `AGENTS.md` / `DEBUG-LOG.md` applies: nothing is a
hardware claim unless it has evidence next to it.

Raw evidence backing the call-duration/outcome tables below:
`session-2026-09-18-calls.json` (full call records from `GET /api/calls`)
and `session-2026-09-18-server-log-excerpt.txt` (WS accept/open/close and
keepalive-timeout lines from the server log), both in this directory.

## Hardware session log (this board, this host, 2026-09-18)

- Board enumerated as `/dev/ttyUSB0` (CH340), same board type as `DEBUG-LOG.md`
  (ESP32 DevKitC clone). User confirmed board is an ESP32 DevKit V1.
- Flash: succeeded on the 4th attempt. First 3 attempts hit
  `Invalid head of packet (0x70)` (matches the known flaky-CH340-RX-line
  quirk in `DEBUG-LOG.md`); a fresh BOOT+RST dance right before the esptool
  call fixed it. `write_flash` verified all 3 images by hash
  (bootloader/partition-table/app, 1.33 MB app image).
- Serial monitoring via `cat`/`stty` and even `pyserial` was extremely
  unreliable on this host: reads alternated between 0 bytes, single-fragment
  spam in the multi-MB range (939,154x repeats of a 5-byte fragment
  observed — physically impossible at 115200 baud, ~14 KB/s max), and
  occasionally real `ESP_LOG` lines. **New quirk (not in the original
  DEBUG-LOG, may be specific to this host/environment): repeatedly opening
  the port appears to interfere with the device's own Wi-Fi/WS session** —
  the one real log line captured showed the app itself getting
  `ESP_ERR_ESP_TLS_CONNECTION_TIMEOUT` connecting to the server; once serial
  access stopped, the device connected within the next reconnect cycle and
  stayed connected. Avoid polling serial while trying to observe steady-state
  network behavior — check the server API instead.
- `ufw` was active and blocking inbound 8788/tcp — confirmed via a real
  connection timeout on-device (not "refused", a true timeout, the classic
  DROP-not-REJECT signature) before the rule was added. `sudo ufw allow
  8788/tcp` + `sudo ufw reload` fixed it; verified with `sudo ufw status`
  showing `8788/tcp ALLOW Anywhere` (v4 and v6).
- **Confirmed on hardware**: after the firewall fix, the device connected
  from its real LAN IP (`192.168.29.99`) and stayed connected —
  `GET /api/devices` showed `status: online` with `last_seen` advancing
  every ~30s (matching the firmware's idle-ping interval) for 75+ seconds
  straight, no drops. This is Gate B (network loop) re-verified live on
  this exact board/flash, not just the CLI simulator.
- **Gate A attempted (S25, echo mode): BT/HFP mechanics work, audio relay
  does not.** Phone paired with `ESP_HFP_HF`, placed/answered several calls,
  each one completed cleanly at the BT layer (mSBC negotiated — the firmware
  hard-rejects CVSD/8kHz, so a completed call proves mSBC came up).
  **No echo was audible.** Root cause found from `frames` counter deltas
  server-side (see next bullet) — not a protocol bug, an ESP32 hardware
  constraint.

### Open problem: Wi-Fi is nearly starved during active HFP/SCO audio

Evidence (`device.frames` delta from `GET /api/devices`, before vs after
each call):

| Call duration | Frames delivered | Expected @ 50fps | % delivered |
|---|---|---|---|
| 25s | ~17 | ~1250 | ~1.4% |
| 20s | (small) | ~1000 | low |
| 33s | 22 | ~1650 | ~1.3% |

ESP32 (classic, not S3/C3) has **one 2.4GHz radio shared by Wi-Fi and
Bluetooth**. Active HFP/SCO audio has tight real-time scheduling
requirements; the softing-coexistence arbiter appears to grant Wi-Fi so
little airtime while SCO is active that only the first ~160-440ms worth of
audio (the size of `s_in_ring`, 8 frames) gets through before the ring
saturates and the rest silently drops. That's consistent with why the
"echo path passed on hardware" claim in the original `DEBUG-LOG.md` — from
a different host/session — may not generalize; it's possible that session's
conditions (different RF environment, different phone/router) happened to
be more forgiving, or the claim didn't test a long enough window to notice.

**Two things tried this session, both firmware-only, both reverted or kept
per evidence:**

1. **Kept**: `send_text()`/`send_audio_frame()` no longer block forever on
   a stalled Wi-Fi send (`esp_websocket_client_send_text(..., portMAX_DELAY)`
   → bounded 15ms timeout for audio, 2000ms for control messages; a new
   `s_net_send_drop` counter tracks drops, surfaced in the `STATS` log line).
   This prevents one stalled send from wedging the whole net_tx_task
   forever, but does NOT fix the underlying starvation — frame counts stayed
   just as low after this fix.
2. **Tried and reverted**: `esp_coex_preference_set(ESP_COEX_PREFER_WIFI)`
   (biases the radio arbiter toward Wi-Fi at BT's expense). **Made it
   measurably worse** — 3/3 trials on this build dropped the WS gateway
   connection within ~0.5-0.6s of call start (vs 11-33s on the default
   "balance" policy), even though the BT/phone call itself stayed up fine
   (user confirmed 13s of actual talk time on one trial). Reverted; do not
   re-try this exact change without new evidence. Left as a documented
   comment in `main.c` at the call site.

**Not tried**: switching the WS audio payload from JSON+base64 to raw
binary frames (~30-35% smaller per frame). Discussed and explicitly
deferred — the loss rate (~95%+) is too severe for a ~30% size cut to be
a plausible full fix, though it's cheap enough to be worth trying later if
someone wants to chip away at this further. Would touch `main.c`,
`callbox/gateway.py`, `tests/test_audio_gateway.py`, and
`scripts/device_simulator.py` (all currently assume the JSON+base64
`{"type":"audio", "pcm16": "<base64>"}` message shape).

### It is NOT purely a fixed ESP32 hardware wall — RF environment matters a lot

**User reported this exact board successfully echoed audio this morning at
a different physical location/router.** That rules out "ESP32 classic simply
cannot do Wi-Fi+BT well enough for this, full stop" as the sole
explanation — coexistence contention is a real, generic ESP32 constraint,
but its *severity* clearly depends on the RF environment, and it can
apparently work well enough somewhere else.

Investigated this host's Wi-Fi environment (`nmcli`, `iw dev ... info`):
this laptop's own Wi-Fi adapter is on `Knowbuild` at **5220 MHz (5GHz,
channel 44)** — the ESP32 cannot join that at all (2.4GHz-only chip). There
are at least **two separate physical APs both broadcasting `Knowbuild` on
2.4GHz channel 1** (different security settings — likely different
mesh/repeater nodes), plus an unrelated strong network
(`Airtel_jagr_6010`, channel 1) crowding the same channel. The ESP32 must
be joining one of the 2.4GHz nodes, and there is no way to see from this
laptop which one, or its actual signal quality at the board's location —
this laptop's antenna and physical position tell you nothing about the
ESP32's.

**Tried moving the board physically closer to a 2.4GHz `Knowbuild` node —
made things measurably WORSE, not better:**

| Location | Calls | Durations | Outcomes |
|---|---|---|---|
| Original spot | 7 calls | 0.5s–33s | 3x "Completed" (11-33s), rest "Gateway disconnected" |
| Moved closer to a 2.4GHz AP | 5 calls | 0.29s–1.8s | 0x "Completed", all "Gateway disconnected"; one 63-second phone call produced **no call record at all** (WS never even registered `call.start`); server log showed a rapid connect/fail loop with `keepalive ping timeout` even outside of any call |

This is a clean, consistent negative result, not noise (5/5 short failures
vs 3/7 successes at the original spot). The new location is worse for
whatever reason — possibly a weaker/more distant/overloaded 2.4GHz repeater
node, new obstruction, or just a worse RF environment generally. **Board was
left at the "moved closer" location at end of session — move it back to the
original spot before the next test.**

### Next steps, in order

1. **Test the actual "worked this morning" location/router again as a
   direct control**, on this same reverted-baseline firmware, before
   changing anything else. If it reliably works there and reliably fails
   here, the problem is this location's RF environment (router placement,
   channel congestion, or which physical `Knowbuild` node the ESP32
   associates with), not the firmware or a fundamental chip limit.
2. If a good location is confirmed: get real signal-strength visibility for
   the ESP32 itself (not proxied through this laptop) — e.g. a phone Wi-Fi
   analyzer app at the board's exact location, or a cleaner serial capture
   of the RSSI ESP-IDF logs at boot (see the serial-port-interferes-with-
   Wi-Fi quirk above — a laptop-side USB-to-serial dongle, not repeated
   `pyserial` opens on this same interface, might avoid that).
3. Consider testing against a router with a manually fixed, unambiguous
   2.4GHz channel (1, 6, or 11, whichever is least crowded per the `nmcli`
   scan) rather than a mesh network with multiple same-SSID nodes.
4. Only after a location is confirmed reliable: revisit the coexistence /
   bandwidth-reduction firmware ideas (both deferred above) if audio is
   still marginal there.
5. Gate C (failure matrix) and Gate D (agent mode) are still blocked behind
   getting real audio through reliably — no point running them until this
   is resolved.

### Round 2: clean phone hotspot + a real architecture bug fix — neither solved it

Continued past the "Next steps" above in the same session, in this order:

1. **Clean phone-hotspot control test.** Connected this laptop's Wi-Fi to a
   phone hotspot (`Shantanu's S25`, 2.4GHz channel 11, WPA2) instead of
   `Knowbuild`, updated `CALLBOX_PUBLIC_ORIGIN`/`CONFIG_CB_SERVER_URI` to
   match the laptop's new hotspot IP, reflashed. This is about as clean an
   RF environment as this session could produce — single AP, no mesh
   ambiguity, no channel congestion, ESP32 and phone physically together.
   **Still failed just as fast**: 2 calls, 0.29s and 0.3s, both "Gateway
   disconnected". This result undercuts the pure-RF-congestion theory from
   the section above — a clean hotspot should have been close to a
   best-case environment, and it wasn't better than the congested
   `Knowbuild` network.
   - Side note: `nmcli`'s saved `Knowbuild` connection auto-reconnected and
     silently pulled the laptop off the hotspot mid－session once already
     (`connection.autoconnect` was `yes`); disabled it
     (`nmcli connection modify "Knowbuild" connection.autoconnect no`) to
     keep the laptop on the hotspot. **Re-enable it when done testing.**
2. **Found and fixed a real architecture bug**: `bridge_on_audio_up()` /
   `bridge_on_audio_down()` are invoked directly from the raw Bluedroid HF
   client callback (`hfp_cb`, registered via
   `esp_hf_client_register_callback`) and were calling `send_simple()` —
   network I/O — synchronously from that BT stack callback thread. This
   directly violates this codebase's own documented rule ("BT callbacks
   only queue and free; JSON/base64/network work happens in dedicated
   tasks", `docs/ARCHITECTURE.md`). Blocking the Bluedroid task on a
   network send (up to 2s with the earlier timeout fix, unbounded before
   that) right as a call starts/ends is a very plausible source of
   instability, independent of Wi-Fi RF quality. Fixed by deferring both
   sends to `net_tx_task` via `s_pending_call_start`/`s_pending_call_end`
   flags (`bridge_on_audio_up`/`down` now only set a flag; the actual
   `send_simple()` call happens at the top of `net_tx_task`'s loop).
   Build-verified clean, reflashed.
   - **Still did not fix it**: 3 calls on this build, 1.1s / 0.4s / 0.4s,
     all "Gateway disconnected". This is a real bug worth having fixed
     regardless (the BT-callback-blocking rule is there for good reasons
     even if it wasn't the dominant cause here), but it was not the root
     cause of the call-instability symptom.

**Updated picture**: four independent fixes/environment changes tried this
session (network-send timeout — kept; coex preference — reverted, made
worse; physical relocation — worse; clean hotspot — no better; deferred
BT-callback sends — no better). All four produce the *same* failure
signature: BT audio comes up fine, WS gateway dies ~0.3-2s later, nearly
every time, regardless of what was changed. That consistency across so many
varied conditions suggests the actual cause has not been identified yet —
it is very likely NOT primarily Wi-Fi RF quality (the clean hotspot result
rules that out as the dominant factor) and NOT the BT-callback-blocking bug
either (fixed, no change). Something else is triggering a near-immediate
gateway disconnect specifically when real HFP/SCO audio goes active.

**User reports a data point from earlier successful testing (this morning,
at home)**: echo was audible but at **very low volume**, not full-volume
audio. This is consistent with (not a separate bug from) everything above —
`codec_out_task` fills ring underflow with clean digital silence rather
than dropping/glitching, so a mostly-starved output stream reaches the
phone as continuous audio that is *mostly silence with sparse real
snippets*, which can read as "quiet" rather than obviously broken. Home's
RF/timing conditions were apparently good enough to avoid killing the WS
connection outright (unlike every environment tested in this session) but
still nowhere near good enough for full audio throughput. This does NOT
mean the "low volume" and "instant disconnect" symptoms have different
root causes — more likely the same underlying audio-starvation/instability
mechanism at different severities, and home happened to sit on the
survivable side of whatever threshold this session's environments did not.

**Best lead for a control test**: of everything tried, only the *original*
`Knowbuild` location/AP (session start, before any relocation) produced
any "Completed" calls (3 of them, 11-33s each) — every other location or
network tried this session (relocated-closer, clean hotspot) produced 0
completed calls. Re-testing there, on the current (deferred-BT-callback-
fix) firmware, is the most promising next diagnostic step, not a new
location.

**Session state at handoff**: board is flashed with the deferred-BT-
callback-fix firmware, currently Wi-Fi-configured for `Shantanu's S25`
hotspot (`sdkconfig` — real device on `CONFIG_CB_SERVER_URI =
ws://10.130.235.174:8788/ws/device`). This laptop is also currently on
that hotspot with `Knowbuild` autoconnect disabled. **Before the next
session**: re-enable `Knowbuild` autoconnect
(`nmcli connection modify "Knowbuild" connection.autoconnect yes`) if this
laptop should go back to normal Wi-Fi, and reflash `sdkconfig` back to
`Knowbuild` credentials if retesting at the original location (see the
"What changed this session" section below for exact SSID/server-URI
values used at each location tested).

## What changed this session

- Fixed a real (non-hardware) test bug: `tests/test_realtime.py` hardcoded
  an absolute date that had drifted into the past; `python scripts/verify.py`
  now passes ALL GATES on a fresh `.venv`.
- Added playout/decode health metrics to `callbox_hfp/main/main.c`
  (`s_msbc_decode_fail` counter + a 5 s `stats_task` logging ring
  drop/underflow counts) — this was "Next steps #4" in `DEBUG-LOG.md`.
  **Build-verified** (not hardware-verified): installed ESP-IDF v5.5.5 +
  pinned Python 3.13 on this machine (`~/esp/esp-idf`, `~/esp/idf-py`) and
  ran a clean `idf.py build` — zero errors, zero warnings, and
  `stats_task`/`s_msbc_decode_fail` confirmed present in the linked binary
  via `nm`. Still never run on real hardware.
  - Cosmetic bug noticed while building: `callbox_hfp/CMakeLists.txt` has
    `project(callbox_dev)` (copy-paste from the other project folder), so
    the built binary is named `callbox_dev.bin`/`.elf` instead of
    `callbox_hfp.bin`. Harmless — flash args reference it correctly either
    way — but worth a one-line fix (`project(callbox_hfp)`) if you touch
    that file.
- `.env`: `CALLBOX_HOST=0.0.0.0` (was `127.0.0.1`, unreachable from the
  ESP32's Wi-Fi) and added `CALLBOX_PUBLIC_ORIGIN` for the LAN IP, matching
  the "ufw / LAN IP" lesson in `DEBUG-LOG.md`.
- Port moved from 8787 to **8788** — 8787 on this machine is held by an
  unrelated tool (`headroom-ai proxy`), not a stale CallBox instance.
- Server is running now: `http://192.168.29.65:8788` (also reachable at
  `127.0.0.1:8788`). Confirmed both origins return 200.
- Gate B (synthetic network loopback, `scripts/device_simulator.py`)
  re-verified clean on this host/port: 100/100 exact frame matches,
  interrupt ack'd, text tools worked, temp credentials revoked.
- Provisioned an ESP32 device record ahead of time:
  - `device_id = dev_a2735d20cf25481d97`
  - token: shown once in this conversation only — copy it from chat
    scrollback into `sdkconfig` (`CONFIG_CB_DEVICE_ID` /
    `CONFIG_CB_DEVICE_TOKEN`) before first flash. It is NOT saved in any
    repo file (device tokens must never be committed).
  - If it's lost, revoke and reprovision: `POST /api/devices/{id}/revoke`
    then a fresh `POST /api/devices`.

## Not done — needs your input or the hardware itself

- ~~ufw / firewall~~ — DONE this session: `sudo ufw allow 8788/tcp` +
  `sudo ufw reload`, confirmed live with `sudo ufw status`.
- ~~ESP-IDF toolchain~~ — DONE this session. Installed at `~/esp/esp-idf`
  (v5.5.5) with a pinned Python 3.13 venv at `~/esp/idf-py`. `idf.py build`
  in `callbox_hfp/` is clean (0 errors, 0 warnings). To rebuild:
  ```bash
  export IDF_PATH=$HOME/esp/esp-idf IDF_PYTHON_ENV_PATH=$HOME/esp/idf-py
  source $IDF_PATH/export.sh
  cd firmware/esp32-hfp-bridge/callbox_hfp && idf.py build
  ```
  Flashing still needs the board connected and the real Wi-Fi/device
  secrets set in the LOCAL `sdkconfig` (see README's Build/Secrets
  sections) — that part was intentionally left for the hardware session.
- Confirm which LAN IP the ESP32's Wi-Fi will actually be able to reach —
  this host has two interfaces on `192.168.29.0/24`
  (`wlp0s20f0u5=192.168.29.65`, `enp0s31f6=192.168.29.125`). `.env` is set
  to `.65`; change `CALLBOX_PUBLIC_ORIGIN` and `CONFIG_CB_SERVER_URI` together
  if `.125` (or a different network entirely) turns out to be the right one.

## Gate C failure matrix (run once echo mode is re-verified — Gate B-hardware)

From `firmware/README.md`. Record device/OS per row; none of these need AI
config, only the echo-mode bridge.

| # | Case | How to trigger | Expected |
|---|---|---|---|
| 1 | Phone lock/unlock | Lock the S25 mid-call | Audio path survives or call ends cleanly, no crash |
| 2 | Bluetooth disconnect | Toggle BT off on phone mid-call | `bridge_on_audio_down` fires, `call.end` sent, server shows call ended |
| 3 | Wi-Fi loss | Disable the AP or move ESP32 out of range | `on_wifi_event` reconnect logic fires, no crash; check reconnection actually resumes |
| 4 | Server stop | `kill` the callbox process mid-call | ESP32 WS client should detect disconnect and not spin/crash |
| 5 | Queue under/overrun | Force silence (mute) for >1s and check `STATS` log line for `underflow`; saturate input by talking continuously and watch `in(drop=...)` | Counters increase, no crash, audio stays reasonable |
| 6 | Call waiting | Second incoming call while bridge call active | Defined behavior, not a crash |
| 7 | Phone restart | Reboot the S25 mid-call | Clean disconnect detected |
| 8 | Box restart | Power-cycle the ESP32 mid-call | Clean reconnect, no stuck `device_busy` (see DEBUG-LOG bug 3) |
| 9 | Internet loss | Cut the server's upstream internet (LAN stays up) | Echo mode should be unaffected (no internet dependency); agent mode should fail gracefully |
| 10 | Revoked token | `POST /api/devices/{id}/revoke` while connected | Gateway force-closes via `revoke()`; ESP32 should not retry a revoked token in a hot loop |
| 11 | Long call | Multi-minute continuous call | No stack overflow (watch the `nettx`/`codecin` tasks specifically — that's where the prior overflow was), no memory growth in server logs |
| 12 | Human takeover | Mid-agent-mode call, hang up / return to handset | `bridge_on_audio_down` -> `call.end`; no orphaned `generation_task` server-side |

## Quick commands (this host, this session's port)

```bash
# server status
curl -s http://127.0.0.1:8788/api/devices -H "Authorization: Bearer $(grep '^CALLBOX_ADMIN_TOKEN=' .env | cut -d= -f2-)"
curl -s "http://127.0.0.1:8788/api/calls?limit=3" -H "Authorization: Bearer $(grep '^CALLBOX_ADMIN_TOKEN=' .env | cut -d= -f2-)"

# revoke + reprovision the lab device if the token was lost
curl -s -X POST http://127.0.0.1:8788/api/devices/dev_a2735d20cf25481d97/revoke \
  -H "Authorization: Bearer $(grep '^CALLBOX_ADMIN_TOKEN=' .env | cut -d= -f2-)" \
  -H 'Content-Type: application/json' -d '{}'
curl -s -X POST http://127.0.0.1:8788/api/devices \
  -H "Authorization: Bearer $(grep '^CALLBOX_ADMIN_TOKEN=' .env | cut -d= -f2-)" \
  -H 'Content-Type: application/json' -d '{"name":"ESP32 HFP lab","kind":"esp32-lab"}'
```
