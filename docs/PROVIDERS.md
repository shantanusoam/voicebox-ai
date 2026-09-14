# Optional speech provider

## Implemented adapter, unperformed live test

`callbox/providers.py` contains a backend-only OpenAI adapter. HTTP contract tests use mocked responses. No API key was supplied or billed request made during this build. A configured key is not a verified connection; the UI labels this distinction.

Defaults are configurable, not a claim about the cheapest or newest models:

| Setting | Default | API |
|---|---|---|
| OPENAI_STT_MODEL | gpt-4o-mini-transcribe | POST /v1/audio/transcriptions |
| OPENAI_INTENT_MODEL | gpt-4.1-mini | POST /v1/responses with strict structured output |
| OPENAI_TTS_MODEL | gpt-4o-mini-tts | POST /v1/audio/speech |
| OPENAI_VOICE | coral | Speech voice |

Official reference documentation consulted for the adapter:
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/docs/guides/text-to-speech
- https://developers.openai.com/api/docs/guides/structured-outputs

Check model access and supported arguments for your account at deployment. This is not the Realtime API, SIP integration, or an OpenAI-hosted telephone service.

## Enable locally

1. Copy `.env.example` to `.env`.
2. Put your API key in `OPENAI_API_KEY`. Never paste a secret into website source or commit it.
3. Restart `python -m callbox`.
4. In the playground, select OpenAI processing and explicitly consent to synthetic-content processing.
5. Use Chrome/another supported browser on localhost or HTTPS, grant microphone permission and record one short turn. Stop manually or wait for the 20-second UI cap.
6. Verify transcript, tool outcome, generated speech, actual provider usage/cost, and saved local state. Repeat with Hindi/Hinglish and interruptions before drawing quality conclusions.

The default local mode requires neither a microphone nor an LLM API. Browser read-aloud is a separate optional browser/OS speech service and is not guaranteed to be offline.

## Guardrails and limits

The model may classify an otherwise unknown request. It cannot bypass the booking state machine, execute arbitrary tools, invent a calendar confirmation or change clinical treatment. The intent schema includes only narrowly enumerated actions. Names, selected slots and final confirmation stay deterministic.

The adapter has timeouts and explicit error mapping; it does not retry paid requests automatically. HTTP input is bounded at 4 MB and supported MIME types. Audio is transient in application memory. Transcripts, names and action results persist in SQLite. Provider-side storage/retention is governed by the provider configuration and account terms; the project does not promise zero upstream retention. The Responses request sets `store:false`, which is not a blanket retention guarantee for every upstream system.

For WS agent mode, generated speech must be mono PCM16 WAV. The lab normalizes it to 16 kHz and outputs 20 ms frames. Replace the simple resampler and benchmark scheduling/audio quality before real phone use.

## Alternative providers

Implement the same asynchronous `transcribe(audio, mime)`, `classify(text, today)` and `speak(text, wav=False)` interface, with validation, sanitized errors, explicit consent and deterministic tool permissions. Add contract tests before replacing the injected provider in `create_app`. Exotel, Plivo, Twilio, SIP and HFP are transports, not interchangeable speech-model adapters. None is wired up here.
