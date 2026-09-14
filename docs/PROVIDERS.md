# Optional speech providers

Two adapters implement one interface - `transcribe(audio, mime)`,
`classify(text, today)`, `speak(text, wav=False)` - so `agent.py` and
`gateway.py` never learn which one is configured. `CALLBOX_PROVIDER` selects
between them (`openrouter`, `openai` or `none`). Contract tests use a mocked
transport and make no paid calls.

A configured key is not a verified connection. The UI labels that distinction,
and `evals/live_smoke.py` is the only thing here that spends money.

## OpenRouter

Enabled with `CALLBOX_PROVIDER=openrouter` and `OPENROUTER_API_KEY`.

| Setting | Default | Route |
|---|---|---|
| OPENROUTER_STT_MODEL | google/gemini-3.8-flash | POST /chat/completions with an `input_audio` part |
| OPENROUTER_INTENT_MODEL | google/gemini-3.8-flash | POST /chat/completions with a strict `json_schema` |
| OPENROUTER_TTS_MODEL | openai/gpt-audio-mini | POST /chat/completions, streamed, `modalities:["text","audio"]` |
| OPENROUTER_VOICE | alloy | Speech voice |
| CALLBOX_MAX_TURN_COST_USD | 0.05 | Per-turn ceiling, enforced against the cost OpenRouter reports |

### Three facts that are easy to get wrong

These were established by probing the live API, not by reading a table:

1. **`/audio/transcriptions` and `/audio/speech` exist but are unusable here.**
   Both routes accept a request and validate a schema, but no model id in the
   catalogue resolves for either - `openai/tts-1`, `openai/gpt-4o-mini-tts`,
   `google/gemini-2.5-flash-preview-tts` and `elevenlabs/eleven-turbo-v2-5` all
   return `Model ... does not exist`. Both speech directions therefore run
   through `/chat/completions`.
2. **Audio output requires `stream: true`.** A non-streaming request returns
   `400 Audio output requires stream: true`. The adapter accumulates
   `choices[].delta.audio.data` chunks and wraps the result in a WAV header, so
   callers resample it exactly as they would any other provider WAV.
3. **`reasoning: {enabled: false}` is rejected.** Gemini answers `Reasoning is
   mandatory for this endpoint and cannot be disabled.` The adapter sends
   `reasoning: {effort: "low"}`, which is accepted and measured at zero
   reasoning tokens.

`speak()` returns 24 kHz mono PCM16 in a WAV container; the gateway resamples
to the 16 kHz device contract.

### The TTS model answers text instead of reading it

`openai/gpt-audio-mini` is a conversational audio model, not a narrator. Given
the reply text as a plain user message it **responds** to it. Asked to say the
agent's own line *"Which date would you like?"* it spoke *"I'm here to help!
Could you give me a bit more detail…"* - so the caller hears a different
sentence than the one written to the transcript.

Two things fix it, and the second is counter-intuitive:

- The text must be delimited inside a single explicit instruction turn
  (`<speak>…</speak>`), not passed as a bare user message.
- **Do not add a system message.** Framing the model as a TTS engine via a
  system role made narration *worse* (1/4 phrases narrated, versus 4/4 with a
  single instruction turn), because the system role reinforces the assistant
  framing it is meant to suppress.

Keep prosody guidance out of that instruction. With a short payload the model
reads the guidance aloud too: "Speak calmly, with short pauses" produced the
spoken output *"confirm short pause short pause short pause"*.

A related limit affects any single-word payload: asked to voice just `1`, the
model prepends an acknowledgement ("Understood. I will read it exactly as you
requested. Here is the text: 1."). Agent replies are full sentences, so this
does not arise in the product, but it does make a TTS-generated test caller
unreliable for short utterances.

### Transcription cannot be trusted to report silence

A chat model asked to transcribe a silent or non-speech clip **invents a
plausible sentence** rather than returning nothing. Measured on one second of
digital silence:

| Model | Returned |
|---|---|
| google/gemini-3.8-flash | "The company's headquarter is located at Washington DC." |
| google/gemini-3.8-flash, effort=low | "We're taking a look at this. It's a high-class, beautiful pl..." |
| google/gemini-3.7-flash | "You must listen to me." |

On a phone line that is fabricated caller text driving a booking state
machine. The empty-string check that suffices for a dedicated speech-to-text
endpoint can never fire here. There are two defences, and the first does not
depend on the model:

- **A deterministic RMS gate** (`callbox/audio.py`, `SILENCE_RMS`). A turn
  quieter than the threshold is refused locally, before any paid request.
  Reference levels: digital silence 0, quiet noise floor ~42, generated speech
  ~3158, threshold 150.
- **A `NO_SPEECH` sentinel** in the transcription prompt, mapped to the
  existing `no_speech` error. It works when the model complies; the gate covers
  when it does not.

Transcription accuracy is also not neutral across fields. In an end-to-end
run the caller's name "Mira Demo" was heard as "Miran Demo" and committed to
the booking verbatim. Names are exactly what speech recognition gets wrong,
and this release writes the transcript straight into the appointment row. A
pilot needs name confirmation or spelling readback before that row is trusted.

The RMS gate only applies to WAV input, which is the gateway path. Compressed
browser uploads (WebM/MP4/OGG) cannot be inspected without a decoder, so they
rely on the sentinel alone. That gap is open.

## OpenAI

Enabled with `CALLBOX_PROVIDER=openai` and `OPENAI_API_KEY`. Unchanged from
0.2.0 and still not live-tested.

| Setting | Default | API |
|---|---|---|
| OPENAI_STT_MODEL | gpt-4o-mini-transcribe | POST /v1/audio/transcriptions |
| OPENAI_INTENT_MODEL | gpt-4.1-mini | POST /v1/responses with strict structured output |
| OPENAI_TTS_MODEL | gpt-4o-mini-tts | POST /v1/audio/speech |
| OPENAI_VOICE | coral | Speech voice |

Check model access and supported arguments for your account at deployment.
This is not the Realtime API, SIP integration, or a hosted telephone service.

## Enable locally

1. Copy `.env.example` to `.env`.
2. Set `CALLBOX_PROVIDER` and the matching key. Never commit `.env` or paste a
   secret into web source.
3. Restart `python -m callbox`.
4. Optionally confirm the provider actually works and what it costs:
   `python evals/live_smoke.py --budget 0.25`. This makes real billed
   requests, stops at the budget, and writes `evals/out/live-smoke.json`.
5. In the playground, select paid processing and explicitly consent.
6. Record one short turn, then check the transcript, tool outcome, generated
   speech, reported cost and saved local state. Repeat in Hindi/Hinglish and
   with interruptions before drawing any quality conclusion.

The default local mode needs neither a microphone nor a paid API. Browser
read-aloud is a separate OS/browser speech service and is not guaranteed to be
offline.

## Guardrails and limits

A model may classify an otherwise unknown request. It cannot bypass the booking
state machine, execute arbitrary tools, invent a calendar confirmation or
change clinical treatment. The intent schema enumerates a closed set of
actions; names, selected slots and the final confirmation stay deterministic.

Both adapters have timeouts, sanitized errors and no automatic retry of paid
requests. HTTP input is bounded at 4 MB and to supported MIME types. Audio is
transient in memory; transcripts, names and action results persist in SQLite.
Upstream retention is governed by your provider account, not by this project.
`store:false` on the Responses request is not a blanket retention guarantee.

Generated speech is normalized to mono PCM16 at 16 kHz and emitted as 20 ms
frames. The resampler is a laboratory linear interpolator with integer
decimation for exact-multiple rates; it runs on a worker thread so it cannot
stall the event loop. Benchmark scheduling and audio quality before any real
phone use.

## Adding another provider

Implement the same three coroutines with validation, sanitized errors,
explicit consent and deterministic tool permissions, add contract tests, then
register it in `build_provider`. Exotel, Plivo, Twilio, SIP and HFP are
transports, not speech-model adapters. None is wired up here.
