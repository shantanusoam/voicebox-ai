# SIP lab

Answer a real SIP call with the CallBox realtime agent, with no carrier, no
DID and no PSTN connectivity. A softphone on this machine registers to a local
Asterisk, which forks the call audio to `scripts/sip_bridge.py` over
AudioSocket. The bridge runs the same engine, tools and booking guarantees the
browser console uses.

This proves the transport. It is **not** a phone service: reaching a real
number requires a licensed SIP trunk from a carrier.

## Why Asterisk and not FreeSWITCH

FreeSWITCH is the better long-term choice for a multi-tenant media platform.
For this POC Asterisk wins on one specific point: `app_audiosocket` is built in
(verified present in the image), while FreeSWITCH needs `mod_audio_stream` or
`mod_audio_fork` compiled from source - neither ships in the standard image.
`callbox/telephony.py` is PBX-agnostic, so moving to FreeSWITCH later means
writing a WebSocket variant of `AudioSocketLeg`, not rewriting the engine.

## Run it

```bash
# 1. the AI side
python scripts/sip_bridge.py --port 8090

# 2. the PBX
docker compose -f telephony/compose.yaml up

# 3. register a softphone (Linphone, Zoiper) and dial 1001
#      server    127.0.0.1
#      user      1000
#      password  callbox-lab-secret
```

`1001` reaches the AI receptionist. `1002` is a plain echo test, useful for
separating an audio-path problem from an AI problem.

## Audio reality

A SIP call is 8 kHz G.711; the Realtime API refuses anything below 24 kHz. The
bridge upsamples, which satisfies the API but does not restore information the
narrowband channel never carried. Recognition of names and digits is measurably
worse than the browser path - confirm them by readback or DTMF before writing
them to a booking.
