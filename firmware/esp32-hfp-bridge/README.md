# ESP32 HFP bridge (hardware lab)

Working ESP32 firmware that bridges a phone's HFP call audio to the CallBox
gateway over Wi-Fi/WebSocket. Built and run on real hardware (ESP32 DevKitC
clone, ESP-WROOM-32, Samsung Galaxy S25 as the HFP audio gateway). This folder
is a hardware-lab addition, not part of the validated `firmware/` ring release.
See `DEBUG-LOG.md` for everything learned while debugging, including what is
and is not verified.

## Two projects

| Project | Proves | Status |
|---|---|---|
| `callbox_dev/` | On-device synthetic loopback: Wi-Fi + authenticated WebSocket + `callbox.v1` framing, byte-exact echo verification, interruption, call.end | Passed on hardware, verified server-side (`qa/` evidence in repo root branch) |
| `callbox_hfp/` | Real phone audio. `local_echo` proves HFP/SCO without Wi-Fi; `echo` adds PCM/WebSocket/server loopback; `agent` adds STT/tools/TTS | HFP pairing + mSBC negotiation verified. Sustained network echo is currently unstable on the lab board; use `local_echo` first to isolate Bluetooth from Wi-Fi coexistence. |

## Architecture (callbox_hfp)

```text
S25 (HFP AG) --Bluetooth mSBC--> ESP32 (external-codec callback, 57 B frames)
  -> decode (BlueZ sbc, MSBC mode) -> 120 samples/7.5 ms
  -> assemble 640-byte (20 ms) PCM16LE mono 16 kHz frames
  -> bounded ring (firmware/callbox_audio_ring, host-tested component)
  -> network TX task (single writer, static buffers)
  -> ws://server/ws/device (callbox.v1: hello/token, call.start, audio, commit)
server: echo mode (loopback) or agent mode (STT -> tools -> TTS)
  -> audio.output frames back -> playback ring (epoch-checked)
  -> 7.5 ms clock task -> sbc_encode -> esp_hf_client_audio_data_send -> phone
```

Task split follows `docs/ARCHITECTURE.md` guidance: BT callbacks only queue and
free; JSON/base64/network work happens in dedicated tasks; rings enforce
bounded queues; `playback.clear` epoch discards stale output.

## Build

Requires ESP-IDF v5.5.5 (pinned) and Python 3.13 for the IDF env (3.14 breaks
IDF 5.5 requirements; `uv python install 3.13` works).

```bash
idf.py set-target esp32
# then set locally, NOT in any committed file (see secrets note):
#   sdkconfig: CONFIG_CB_WIFI_SSID / CONFIG_CB_WIFI_PASS
#   sdkconfig: CONFIG_CB_SERVER_URI / CONFIG_CB_DEVICE_ID / CONFIG_CB_DEVICE_TOKEN
#   sdkconfig: CONFIG_CB_CALL_MODE ("local_echo", "local_tone", "echo" or "agent")
idf.py build
```

Flashing on the lab board needs manual download mode (marginal CH340 RX line):
hold BOOT, tap RST, release, then

```bash
python -m esptool --chip esp32 --port /dev/ttyUSB0 --baud 115200 \
  --before no-reset --after no-reset write_flash --flash_mode dio \
  --flash_size 4MB --flash_freq 40m 0x1000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin 0x10000 build/*.bin
```

Retry the connect a few times if it fails with serial noise. After flashing,
tap RST to boot. Logs: `stty -F /dev/ttyUSB0 115200 raw -echo; cat /dev/ttyUSB0`.

## Secrets

Wi-Fi credentials, device IDs and device tokens live only in the LOCAL
`sdkconfig` (git-ignored). They are never committed. Provision a device with
`POST /api/devices` (admin token from `.runtime/admin-token`); the device token
is shown once.

## Vendored components

- `components/sbc/` — BlueZ SBC library 2.0 (GPL-2.0). mSBC decode/encode at
  16 kHz; runs on generic C primitives (no SIMD paths are taken on Xtensa).
  License: `components/sbc/` keeps upstream headers' notices; GPL applies to
  this component. Lab use only; do not ship without a licensing review.
- `components/esp_websocket_client/` — espressif/esp_websocket_client v1.4.0
  (Apache-2.0), vendored because the IDF component manager did not run in this
  environment (see DEBUG-LOG.md); `esp_stubs` dependency removed from its
  CMakeLists (component does not exist in IDF v5.5.5).

## Not implemented / not verified

- CVSD air mode (8 kHz) is rejected with a log error, not transcoded. WBS/mSBC
  is required (S25 negotiates mSBC).
- Agent mode (paid STT/TTS through the server) is implemented on the device
  (RMS speech gate, `audio.commit`, turn gating) but has NOT been verified on
  hardware yet.
- No sample-clock pacing of network TX beyond the SCO arrival rate; no jitter
  measurement; no long-call soak test; no Wi-Fi loss/roaming failure tests.
- Echo cancellation: the phone's AG handles echo; there is no local AEC.


## Voice/echo troubleshooting order

If the phone pairs but you cannot hear the echo, do not start with the AI path.
Use the diagnostic modes in this order:

1. **\`local_echo\`** — no Wi-Fi, WebSocket, JSON, PCM conversion or SBC re-encode. The firmware buffers encoded mSBC packets and returns them to the phone using the same external-codec pattern as Espressif's HFP HF example. Speak from the *remote side of the phone call* and listen for that speech to come back. Logs report SCO RX/TX packet statistics.
2. **\`echo\`** — enables the complete HFP -> PCM -> WebSocket -> server -> PCM -> mSBC path. If local echo works but this fails, focus on Wi-Fi/Bluetooth coexistence and network scheduling, not pairing or the phone's HFP negotiation.
3. **\`agent\`** — only after echo is stable.

The firmware disables Wi-Fi modem sleep in network modes to avoid extra latency while Classic Bluetooth SCO and Wi-Fi share the ESP32's 2.4 GHz radio. It also stops Bluetooth inquiry discoverability after the HFP service-level connection is established while remaining reconnectable.

For a local-echo test set \`CONFIG_CB_CALL_MODE="local_echo"\`, rebuild and flash. You do **not** need the CallBox server or Wi-Fi credentials for that mode. Watch for \`LOCAL_ECHO\` and \`SCO PKTS\` log lines. A high \`rx_none/rx_lost\` count means the SCO radio path itself is unhealthy; good RX/TX counters with audible local echo but failing network echo points at Wi-Fi/WebSocket coexistence.
