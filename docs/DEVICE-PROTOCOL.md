# callbox.v1 device protocol

Endpoint: `ws://127.0.0.1:8787/ws/device` locally. Use authenticated TLS/WSS after a reviewed deployment. A device token belongs in the first message, never in the URL. No raw phone number or SIM API is involved.

## Provision and authenticate

An authenticated operator calls `POST /api/devices` with `{"name":"Front desk lab","kind":"simulator"}`. The response contains a one-time random token; the server stores its hash. `kind` can also be `esp32-lab`, which is a label, not proof of connected hardware.

Within five seconds of opening the socket, send:

```json
{"type":"hello","protocol":"callbox.v1","device_id":"dev_FROM_API","token":"TOKEN_FROM_API"}
```

The server replies with `ready`, including sample rate 16000, frame_bytes 640 and max_turn_ms 15000. A duplicate connection for the same identity is rejected. A revoked token cannot reconnect.

## Start a lab session

```json
{"type":"call.start","mode":"echo","consent":true}
```

`call.started` returns `call_id`, epoch 0 and a textual greeting. Echo mode never calls a paid model. `mode:"agent"` requires a configured provider key; it uses turn-based speech processing. The initial greeting is text: automatic phone playback of that greeting is not implemented.

## Audio framing

- Signed little-endian PCM16; one channel; 16000 samples/second.
- Exactly 320 samples = 640 bytes = 20 milliseconds per message.
- Base64 in `pcm16`; no WAV header in individual frames.
- `seq` starts at 0 and increases by exactly one for every accepted input frame.
- `seq` remains continuous after `audio.commit` and `interrupt`; it resets only on a new call.
- `epoch` starts at 0. Use the new epoch returned by `playback.clear` after interruption.
- Every call-scoped message contains the active `call_id`.

```json
{"type":"audio","call_id":"call_FROM_SERVER","seq":0,"epoch":0,"sample_rate":16000,"channels":1,"pcm16":"BASE64_OF_EXACTLY_640_BYTES"}
```

The placeholder is explanatory, not a runnable audio frame. `scripts/device_simulator.py` produces valid frames and is the executable reference.

In echo mode, each input receives an immediate `audio.output` with the same payload, sequence and epoch. In agent mode, send at least 100 ms and no more than 15 seconds, then:

```json
{"type":"audio.commit","call_id":"call_FROM_SERVER","request_id":"unique-turn-0001"}
```

The server emits `turn.processing`, then `turn.result` with text/actions, then zero or more `audio.output` frames, then `turn.done`. Input and output sequence numbers are separate: output sequence restarts per generated turn. Only one generated turn runs at a time. A valid idempotent retry can return the cached text/actions and `turn.done` without replaying speech.

## Interrupt and finish

```json
{"type":"interrupt","call_id":"call_FROM_SERVER"}
```

`playback.clear` acknowledges the next epoch. Immediately discard queued output and previous-epoch audio. This does NOT undo already committed bookings. Real endpoint playback-clearing behavior still needs hardware tests.

```json
{"type":"call.end","call_id":"call_FROM_SERVER"}
```

Expect `call.ended`. The socket may start another consented test. Disconnect ends the active call and marks the identity offline.

A `ping` message is allowed without an active call and receives `pong`. Send heartbeats more frequently than 45 seconds while idle. The entire connection is limited to 30 minutes. Transport errors are `{"type":"error","code":"...","message":"..."}`; authenticate/revocation/connection-limit failures can close the socket. Frame errors must be reconciled rather than blindly incrementing sequence counters.

## Text diagnostic path

`call.text` with `text`, `request_id` and `call_id` exercises the same tools without audio recognition. The native simulator uses this to test hours and staff-request handling. It is diagnostic software, not speech recognition.

## Hardware integration contract

HFP audio and codecs do not automatically match this wire format. The future phone adapter must negotiate/decode its actual HFP audio, convert sample rate, assemble frames, maintain clocks, serialize network writes outside audio callbacks, enforce bounded queues and play output through the correct uplink path. Neither this protocol nor the host C ring buffer performs those tasks.
