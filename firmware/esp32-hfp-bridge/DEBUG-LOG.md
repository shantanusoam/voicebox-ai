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
