# Voice runtimes and provider routing

CallBox has one business agent and more than one voice runtime. The transport
(browser microphone or SIP AudioSocket), tenant routing, tool policy and booking
guarantees do **not** change when a speech provider changes.

```text
Browser / SIP
      |
      v
Voice runtime
  |-- openai    -> OpenAI Realtime
  `-- pipeline  -> local VAD -> STT -> tool LLM -> TTS
                         |          |          |
                    Groq/Sarvam  OpenRouter  Sarvam
                    /OpenAI      /OpenAI     /OpenAI
      |
      v
Orchestrator -> deterministic tools -> SQLite (future Postgres/MCP)
```

The provider-neutral pipeline exists to test the cost/quality trade-off without
paying a managed voice-agent orchestration fee. It is a POC runtime, not yet a
production full-duplex media engine.

## Native OpenAI Realtime

```env
CALLBOX_VOICE_RUNTIME=openai
OPENAI_API_KEY=...
CALLBOX_REALTIME_MODEL=gpt-realtime-2.1-mini
```

This keeps OpenAI's server VAD, speech understanding, generation and barge-in.
`callbox/realtime.py` still resolves every tool call through the same
`Orchestrator`; the model cannot write the database directly.

## Low-cost component pipeline

A useful India-first experiment is:

```env
CALLBOX_VOICE_RUNTIME=pipeline
CALLBOX_PIPELINE_STT=groq
CALLBOX_PIPELINE_LLM=openrouter
CALLBOX_PIPELINE_TTS=sarvam

GROQ_API_KEY=...
OPENROUTER_API_KEY=...
SARVAM_API_KEY=...

GROQ_STT_MODEL=whisper-large-v3-turbo
SARVAM_TTS_MODEL=bulbul:v3
SARVAM_VOICE=shubh
SARVAM_LANGUAGE=hi-IN
```

The runtime then does:

1. local deterministic RMS VAD/end-of-turn detection;
2. transcription through the selected STT adapter;
3. a tool-calling text model with only the current phase's tools;
4. policy/validation/audit in `callbox/orchestrator.py`;
5. TTS through the selected speech provider;
6. PCM output through the existing browser/SIP transport.

The input reader stays live while STT/LLM/TTS are running. If the caller starts
speaking over playback, queued audio is cleared and stale generated audio is
suppressed. Already-sent upstream requests cannot be un-billed or reliably
cancelled, and committed business writes are not rolled back by an interruption.

## Provider combinations

Supported component selections in this release:

| Component | Providers |
|---|---|
| STT | Groq Whisper, Sarvam Saaras, OpenAI transcription |
| Tool LLM | OpenRouter, OpenAI-compatible chat completions |
| TTS | Sarvam Bulbul, OpenAI speech |

The defaults intentionally favour a low-cost experiment rather than claim a
production winner. Measure with real, consented test audio before choosing a
provider. Names, dates and phone digits deserve explicit read-back because
narrowband telephony makes them harder than browser audio.

## What is deliberately not hidden

- The pipeline is sequential STT -> LLM -> TTS; it will not match native
  speech-to-speech latency until streaming component adapters are added.
- Local VAD is an energy threshold, not semantic end-of-turn detection.
- Sarvam/Groq endpoints are adapter contracts only until live-smoke tested with
  your own account and current model access.
- Asterisk/SIP in this repository is still a local transport lab. A real PSTN
  number requires a licensed carrier/SIP trunk and applicable KYC.
- Provider billing is upstream. CallBox prevents automatic retries and enforces
  the reported LLM cost ceiling where available, but cannot reverse a request
  already accepted by a provider.

## Why the orchestrator stays in the middle

Do not attach arbitrary MCP servers directly to the voice model when they can
mutate business data. Expose a normal function tool to the model, validate and
authorise it in `Orchestrator`, then let its handler call MCP downstream. That
keeps risk tiers, write serialization and audit in one trust boundary.
