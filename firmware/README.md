# CallBox hardware laboratory

## What is real in this folder

A portable C11 queue for **normalized** 16 kHz mono PCM16 frames. `make test` compiles it on the host and checks FIFO behavior, bounded capacity, underflow silence, interruption epochs and 10,000 wrap cycles. It is not an ESP32 firmware image and contains no functioning Bluetooth driver.

```bash
make -C firmware test
```

`include/callbox_audio_ring.h` documents the API. The queue stores eight 640-byte frames (160 ms). Full queues reject new input; empty queues return silence; interruption clears queued frames and changes epoch. It allocates no heap memory. The integrator must provide appropriate synchronization if producer and consumer run in separate tasks or interrupts.

## Hardware proof plan

Record the exact phone model/OS, original HFP-capable ESP32 board/module, power arrangement, network connection and ESP-IDF version. Do not assume all chips branded ESP32 support the same Classic Bluetooth profiles. Do not purchase a production run based on this repository or the concept image.

Begin with the official Espressif HFP-client example and API documentation for your selected chip and pinned SDK:
https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/bluetooth/esp_hf_client.html

The exact callback signatures, supported audio mode, codec path and build configuration must come from that SDK, not guessed function names copied across versions. This release was not cross-compiled against ESP-IDF and no board was connected.

### Gate A: local handset loop

Pair as a hands-free endpoint with an owned test phone. Receive a consented test call, answer through the supported control API, capture incoming audio and transmit a known quiet test tone in the opposite direction. Confirm which direction reaches the caller. Prove reject/hangup and return-to-handset behavior. Keep the AI and network out of this experiment.

### Gate B: network loop

Decode/normalize actual HFP audio, often with a format different from this project's 16 kHz frame contract. Batch it into 640-byte PCM frames with sample-clock pacing. Send through an authenticated network task to the CallBox echo gateway. Never block a Bluetooth audio callback on HTTP, TLS, JSON or a model response. Decode returned frames into a bounded playback queue. Use secure provisioning; do not hardcode customer tokens into shared firmware images.

Suggested task split:

```text
HFP callbacks -> codec / input ring -> network TX task
network RX -> epoch check / playback ring -> codec / HFP output
call control task -> lifecycle, timeout, disable switch, handset recovery
```

The supplied ring helps one piece of this design. Ethernet versus concurrent Wi-Fi/Bluetooth should be measured, not presumed better without testing the selected board.

### Gate C: failures before AI

Verify at least: phone lock/unlock; Bluetooth disconnect; Wi-Fi loss; server stop; queue under/overrun; call waiting; phone restart; box restart; internet loss; revoked token; long call; human takeover. Record the device/OS combination for each run. Targets such as a stable 30-minute loop are engineering gates, not measured claims in this release. Confirm safe output volume and suppress test tones during ordinary use.

### Gate D: one supervised voice workflow

Only after both audio directions and recovery work, enable a short consented synthetic voice test through the paid adapter. Measure complete-turn latency, intelligibility, packet gaps and task completion. Build endpoint playback cancellation and human fallback before an unsupervised trial. The server's epoch mechanism cannot force a phone to return audio to its handset.

## Not a SIM farm or carrier replacement

No SIM gateway, spoofed caller-ID function, autodialer or commercial routing bypass is supplied. A business deployment must satisfy actual carrier/device/telecom requirements. Multi-line operations should evaluate a licensed carrier/PBX adapter separately rather than extrapolating from a single-phone experiment.
