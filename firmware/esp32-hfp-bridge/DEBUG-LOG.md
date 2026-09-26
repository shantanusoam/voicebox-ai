# ESP32 hardware lab — debug log and handoff

Everything an engineer or agent needs to continue from where this stopped.
Written while the work was fresh; every claim below corresponds to a real
execution on 2026-09-17/18 unless marked otherwise. House rule from
`AGENTS.md` applies: nothing here is verified unless stated with evidence.

## Lab record (required by firmware/README.md hardware proof plan)

- Board: ESP32 DevKitC clone, ESP-WROOM-32 (chip rev v3.1), MAC
  4 MB flash, 40 MHz crystal, CH340 USB-serial
- Phone: Samsung Galaxy S25 (Android), paired as HFP audio gateway (HFP AG)
- Firmware: ESP-IDF v5.5.5 (shallow clone at `~/esp/esp-idf`), IDF Python env
  at `~/esp/idf-py` (Python 3.13.15 via uv)
- Server: `~/voicebox-ai` at commit of branch `hardware-lab/esp32-hfp-bridge`
- Host: Arch Linux, Python 3.14 system (too new for IDF 5.5 — hence uv 3.13)

## Gate status

| Gate | Claim | Evidence |
|---|---|---|
| A: local handset loop | PASSED | ESP32 as HF, S25 paired (SSP auto-confirm), call answered from headset via `ac` console command, `--audio state connected_msbc`, caller heard their own voice (example echo), remote hangup clean |
| B: network loop (synthetic) | PASSED | `callbox_dev` firmware: on-device frames over Wi-Fi->WS, 150 frames, server reports call Completed; earlier CLI simulator run: 50/50 byte-exact (`qa/my-loopback.json`) |
| B: network loop (real voice) | PASSED (echo mode) | S25 call audio bridged through server; caller heard own voice returned by the server; server logged the call (6.6 s). One firmware bug found during this test (stack overflow, fixed below); post-fix firmware flashed but NOT yet re-verified |
| B: agent mode | NOT RUN | Device code path exists (RMS gate + commit); needs provider key + supervised test |
| C: failure matrix | NOT RUN | none of the required failure cases executed yet |
| D: supervised AI workflow | NOT RUN | requires OpenAI key server-side, explicit operator opt-in |

## What is on the board right now

`callbox_hfp` build from this folder (echo mode, stack-overflow fix included),
flashed at 08:5x local. Re-pairing the phone was needed once after the FIRST
combined flash (fresh BT state); note `write_flash` does not erase the NVS
partition, so pairing keys normally survive reflash — after the very first
combined flash the phone-side entry had to be forgotten and re-paired.

## Environment setup that worked (host machine)

```bash
# Python 3.13 (system 3.14 is NOT supported by IDF 5.5 tooling)
uv python install 3.13
# IDF toolchain (downloads already in ~/.espressif)
cd ~/esp/esp-idf && IDF_PYTHON_ENV_PATH=~/esp/idf-py ./install.sh esp32
# per shell
export IDF_PATH=$HOME/esp/esp-idf IDF_PYTHON_ENV_PATH=$HOME/esp/idf-py
source $IDF_PATH/export.sh
```

Server for LAN devices (it must be reachable from the ESP32):

```bash
systemd-run --user --unit=callbox-srv2 \
  --property=WorkingDirectory=/home/lol/voicebox-ai \
  --setenv=CALLBOX_HOST=0.0.0.0 \
  --setenv=CALLBOX_PUBLIC_ORIGIN=http://<PC-LAN-IP>:8787 \
  /home/lol/voicebox-ai/.venv/bin/python -m callbox
```

- `CALLBOX_HOST=0.0.0.0` so LAN devices can reach it (default binds loopback)
- `CALLBOX_PUBLIC_ORIGIN` adds the LAN host to the allowlist in
  `callbox/limits.py` (otherwise 403 for every LAN request)
- `ufw allow 8787/tcp` was REQUIRED — ufw was blocking LAN devices (this cost
  an hour of confusion: phone could not reach the server either)
- Provision: `curl -X POST http://127.0.0.1:8787/api/devices -H "Authorization:
  Bearer $(cat ~/voicebox-ai/.runtime/admin-token)" -H 'Content-Type:
  application/json' -d '{"name":"S25 lab ESP32","kind":"esp32-lab"}'`
  (used device: `dev_95741eeebe34440093`, token in local sdkconfig only)

## Hardware quirks learned (this specific board)

1. **Flaky CH340 RX line.** The board transmits fine but esptool sync failed
   with "No serial data received" until several unplug/replug cycles. After
   replug, esptool sometimes reported `Invalid head of packet (0x70)`, then
   connected. Retry loops around `--before no-reset` after a manual BOOT+RST
   dance are the reliable procedure.
2. **Manual download mode is mandatory** on this board: hold BOOT, tap RST,
   release, keep the chip in that state (no power cycle), run esptool with
   `--before no-reset --after no-reset`.
3. **Stale-buffer spam artifact.** The CH340 can resend old buffer content at
   impossibly high rates after glitches (48 MB in 75 s observed). Do not trust
   garbled duplicate lines from this port; verify state server-side instead.
4. Flash writes at 115200 baud, ~45 s per MB, hash-verified OK every time.

## Bugs found while building this, and fixes

1. **`idf.py` component manager never runs in this environment.** Even with
   `IDF_COMPONENT_MANAGER=1`, `idf_component.yml` was ignored (manifest left
   `managed_components` empty; manual `prepare_components` also resolved
   nothing). Root cause not found — workaround: vendor
   `esp_websocket_client` v1.4.0 from the registry zip into
   `components/`. Its `esp_stubs` REQUIRES entry does not exist in IDF
   v5.5.5 and was removed.
2. **Stack overflow in `nettx`** (crash: `***ERROR*** A stack overflow in task
   nettx has been detected`, backtrace corrupted). Cause: 4 KB JSON + ~856 B
   base64 stack buffers in a 6 KB-stack task. Fix in `main/callbox_main.c`:
   `send_audio_frame` now uses `static` buffers (single-writer task) and the
   task stack is 8 KB. This is exactly the "bounded queues, no allocation in
   hot paths" discipline the repo preaches — respect it when extending.
3. **`device_busy` after mid-test reboot.** If the device reboots while the
   server still holds the old socket, the next hello is rejected until the
   server notices the dead connection. Server-side cleanup happens (device
   went back to offline); the WS client retries on its own timer. Consider a
   server-side same-token takeover policy if this becomes annoying.
4. **CH340 stale-buffer echo** (see quirk 3) can masquerade as a firmware
   bug. Cross-check the server API (`/api/devices`, `/api/calls`) before
   debugging firmware based on serial garbage.
5. ESP-IDF examples' `sdkconfig.defaults` (hfp_hf) are the source of truth for
   BT Kconfig names; `BT_HFP_ENABLE` defaults to n and must be set explicitly.
6. `esp_netif_create_default_wifi_sta` returns `esp_netif_t*` — wrap in
   `(void)`, not `ESP_ERROR_CHECK`.
7. `esp_websocket_client_register_events` is `esp_websocket_register_events`
   in v1.4.0.
8. BlueZ sbc: headers are flat (`#include "sbc.h"`), `sbc_encode` wants
   `ssize_t*` (signed) for written; primitives headers
   (`sbc_primitives_*.h`) must be copied too or the build fails in
   `sbc_primitives.c`.
9. mSBC timing: decoded frame = 120 samples = 240 B per 7.5 ms; wire frame =
   640 B = 20 ms; assembly bridge between the two clocks uses a small
   remainder buffer. Output side is clocked by a 7.5 ms esp_timer into
   240-byte encode chunks (4 mSBC frames/s = 57 B each).

## Current state at handoff

- `~/esp/callbox_hfp` — latest source (matches this folder), built and
  flashed (fix included). Awaiting re-test of a real call in echo mode.
- `~/esp/callbox_dev` — synthetic-loopback firmware (superseded by the bridge
  but useful as a minimal network-only test rig).
- Server running as user unit `callbox-srv2` (0.0.0.0:8787), admin token at
  `~/voicebox-ai/.runtime/admin-token`.
- Provider NOT configured: no `.env` exists yet; agent mode will fail at
  `call.start` until `CALLBOX_PROVIDER`/`OPENAI_API_KEY` are set (see
  `docs/PROVIDERS.md`). Echo mode needs no key.

## Next steps, in order

1. Re-test echo mode end-to-end after the stack-overflow fix (pair S25 ->
   call -> verify caller hears echo + server logs the call cleanly).
2. Flip `CONFIG_CB_CALL_MODE="agent"`, set provider key server-side, run ONE
   supervised consented call (Gate D); measure turn latency.
3. Gate C failure matrix from `firmware/README.md` (BT off, Wi-Fi loss, server
   stop, revoked token, long call, etc.), recording device/OS per case.
4. Consider mSBC bad-frame counter metrics + playout stats in logs.
5. Run `python scripts/verify.py` on this repo before merging the branch
   (new hardware code should not regress the L0-L7 gates; the ring component
   is unchanged — confirm `make -C firmware test`).

## Command cheat sheet

```bash
# build
cd ~/esp/callbox_hfp && source ~/esp/esp-idf/export.sh && idf.py build
# flash (manual download mode; see above)
# boot log
stty -F /dev/ttyUSB0 115200 raw -echo; cat /dev/ttyUSB0
# server status
systemctl --user is-active callbox-srv2
TOKEN=$(cat ~/voicebox-ai/.runtime/admin-token)
curl -s http://127.0.0.1:8787/api/devices -H "Authorization: Bearer $TOKEN"
curl -s "http://127.0.0.1:8787/api/calls?limit=3" -H "Authorization: Bearer $TOKEN"
# provider config for agent mode (server-side only)
cp ~/voicebox-ai/.env.example ~/voicebox-ai/.env  # then edit CALLBOX_PROVIDER/OPENAI_API_KEY
# then restart the unit:
systemctl --user restart callbox-srv2
```

## Session addendum (2026-09-26): agent mode reached end-to-end audio

Continuing from the state above, agent mode was taken to first live audio.
Machine was rebooted between sessions: transient units and /tmp vanish — the
server must be restarted (systemd-run command above) and ufw rules persist.

Two more blockers found and fixed today:

6. **The working tree was stuck mid-rebase**, silently serving an OLD
   gateway.py (`receive_text()` only) while the firmware sent binary batches.
   Every first binary batch killed the connection with a suppressed TypeError
   ("Gateway disconnected", no traceback). The binary gateway commit exists
   (e228deb) but the stalled rebase hid it. Lesson: when serial and API
   disagree, check `git status` for a rebase before debugging protocol code.
   Testing now happens from a worktree pinned to the PR #8 merge (8d8b24d).

7. **gpt-4o-mini-tts returns a STREAMING WAV** whose RIFF header declares
   0xFFFFFFFF frames. `wave.getnframes()/rate > 60` then always trips and the
   reply was discarded as tts_format before reaching the phone. Fixed in
   callbox/audio.py by validating the bytes actually read, not the header
   count. (One billed TTS call was used to capture the payload and prove the
   magic bytes: `RIFF\xff\xff\xff\xffWAVE`.)

Also fixed on the device: a playback jitter buffer (24-frame staging, 14-frame
prime, re-prime on starvation) because the downlink arrives in bursts while
SCO runs.

STT now transcribes real speech (one turn transcribed "Hello?" as Urdu
"ہیلو" — language forcing may be worth configuring), the agent replied and
queued staff review correctly, and the AI's generated voice REACHED THE PHONE
audibly. Remaining quality problem, confirmed live and matching the known
issues above: simultaneous SCO + Wi-Fi collapses the downlink (uvicorn
keepalive pings fail under the TTS send loop; playback arrives ~10%
intelligible). Next engineering step: binary DOWNLINK batches (server +
device), then Gate C.

Server for testing: worktree at the PR #8 merge, user unit `callbox-good`,
same env vars as above. The debug PCM dump trick (env-gated write of
committed turns to /tmp) proved invaluable for content diagnosis; keep it in
mind, keep it out of commits.

## Session addendum (2026-09-26, later): binary downlink shipped, RF wall measured

Downlink batching (CBB1) negotiated via hello["downlink"]=="binary" is
implemented both sides with a backward-compatible JSON fallback, paced
sending (first two batches un-paced to prime the device jitter, then 0.92x
real time), and incremental device-side reassembly. Critical heap lesson:
BT+WiFi coexistence leaves ~5 KB free heap; any multi-KB static added to the
audio path breaks Bluedroid's HFP malloc at phone connect (malloc failed
size=4112, largest_block=960 observed). Size every buffer against that budget.

Downlink intelligibility on the S25+ESP32 rig remains ~10% (laggy, 90% word
loss) even with binary batches + 20-frame jitter. Combined with the earlier
8.4 fps echo measurement and today's local_tone bisect conclusion on the
hardware-lab branch ("Bluetooth is healthy, Wi-Fi is not"), the wall is RF:
the shared 2.4 GHz radio starves Wi-Fi while eSCO reserved slots run.
Buffering cannot fix slot starvation. Candidate next levers, in order of
promise:
1. local_tone bisect follow-up: measure pure Wi-Fi throughput during an
   active SCO call (iperf3 from the device) to quantify the ceiling.
2. UDP downlink probe: datagrams avoid TCP head-of-line blocking during
   stalls; still bounded by radio slots, but recovers faster after each.
3. Move the live demo to telephony/ (Asterisk SIP lab): PC-side audio has no
   ESP32 radio constraint and exercises the same gateway/agent stack.
4. Hardware revision only if 1-2 prove the arbitration is the limit.

Serial measurement caveat: the CH340 stale-buffer glitch (48 MB/min of one
repeated line) returned during this session; verify STATS via a fresh
capture or trust server-side counters instead.

## Candidate follow-up (software only; not flashed or heard)

The former receive path accepted two immediate 6-frame CBB1 batches into an
8-frame ring before a separate 20-frame jitter queue. If the playback task
did not drain between those network events, four frames were silently
rejected; `dl_frames` still increased. The 0.92x server sleep also fed audio
faster than the playback clock over a long reply. These are software loss
paths, so the earlier ~10% subjective intelligibility does not yet establish
the physical RF ceiling.

The candidate branch uses one 20-frame playback queue, counts only accepted
frames, logs queue drop/underflow/occupancy and heap, sends at the 20 ms clock,
and adds negotiated CBB2/IMA ADPCM (164 bytes per 20 ms) for a 4x smaller
downlink. Host tests and the gateway eval do not include SCO+Wi-Fi RF. First
flash and compare CBB1 vs CBB2 on the S25, then measure packet arrivals and
RF throughput before declaring a hardware limit or moving to UDP.

## PR #9 hardware validation (2026-09-26): A/B campaign results

Hardware validation of fix/esp32-downlink-playback (commit 2dc0be3), per the
PR's own gate. Server: worktree at 2dc0be3 (SHA
2dc0be3549c72b23ab8bf303db226919ba97a746), firmware: same commit, built with
ESP-IDF v5.5.5 (build clean, 0x148090 bytes, 57% app partition free) and
flashed at 115200 baud. `python scripts/verify.py`: ALL GATES PASSED (69/69
checks incl. the new queue/ADPCM parity tests) on this machine before
flashing.

### Environment traps that cost the first two "failures" (not code bugs)
1. Port 8787 was still held by the previous session's server unit — the PR
   firmware talked to the OLD gateway for two calls. Kill stale units before
   blaming code. The PR server must be the process bound to 8787.
2. The PR worktree starts with a FRESH .runtime DB — the lab device
   (dev_95741eee…) is not registered in it, so hello was rejected as invalid
   credentials and the call had no server at all. Copy the lab DB into the
   worktree before testing.
3. The PC had roamed to Excitel_161095441_5 (5 GHz) while the ESP32 is on
   the 2.4 GHz SSID; the router isolates the bands. Symptom: WS
   ESP_ERR_ESP_TLS_CONNECT from the device, phone-browser probe to the
   server also fails. Fix: keep the PC on the 2.4 GHz SSID. The ESP32 radio
   is 2.4 GHz only.

### Campaign (held constant: S25, 2.4 GHz AP, ~2 m distance, server host, spoken sample)
| Arm | Calls | Result |
|---|---|---|
| local_tone (isolation gate) | 1 | Continuous single-frequency tone on the caller end for the whole call. LOCAL_HFP sent=4127 drop=0 bad=0. Bluetooth mSBC playout exonerated. |
| echo + binary | 1 | No usable voice. Echo couples downlink 1:1 to uplink; round-trip through the coexistence-choked link collapses both (tx_seq advanced ~1.3 fps while the input ring dropped ~90 fps of capture). Echo collapse is an artifact of 1:1 coupling, not of the codec or the queue. |
| agent + binary | 1 | Reply audible with dropouts; WS died mid-call (server-side websockets drain AssertionError during TTS push). |
| agent + adpcm (CBB2) | 3 + 1 instrumented | Call 1: mostly clear, phrase-level drops (caller transcribed ~75% of the reply text). Call 2: no reply — caller hung up 300 ms after the last speech spike, before the 800 ms trailing-silence commit window closed (hang-up race, not a codec failure). Call 3 + instrumented: reply heard. |

### Instrumented agent+adpcm playback (STATS/HEAP during TTS)
- `dl(frames=752, bad=0, stale=0)` — 15 s of TTS delivered with zero bad or
  stale blocks.
- `out(drop=2, under=13, high=20, queued=3..6)` — **98% frame delivery**
  during playback; the residual choppiness is the 13 stall gaps.
- `HEAP free=33.5 KB largest=26.6 KB` during playback (22.6 KB during the
  longer turn) — comfortable, no allocation failures, BT pairing unaffected.
- First-audio delay: TURN_PROCESSING → first audio ≈ TTS generation (~4-5 s,
  OpenAI gpt-4o-mini-tts) + ~0.4 s jitter prime (first two batches un-paced).
  Provider thinking time and playback prime are separate terms.
- Residual uplink capture loss (in drop=78..196 per turn) degrades STT
  transcripts slightly but every instrumented turn transcribed and ran.

### Verdict
The PR's locked playback queue + CBB2 ADPCM downlink turns a ~10%-intelligible
path into a working one: intelligible replies, 98% frame delivery, healthy
heap, no mid-call disconnects in the final arm. Remaining word loss (~2% of
frames + phrase gaps under multi-second radio stalls) is bounded by SCO/Wi-Fi
coexistence on the shared radio, now measured directly rather than inferred.
The remaining levers (UDP downlink probe, SIP/Asterisk demo path) are tracked
in the earlier addendum. No claim of "no lag" is made: dropouts remain audible
under radio stalls.

Verified: firmware build + all software gates at commit 2dc0be3 on this
machine; physical call results as above. The PR stays open for review.
