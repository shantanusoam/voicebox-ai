"""OpenRouter adapter contracts. MockTransport only; no paid calls."""
import asyncio
import base64
import json
import httpx
import pytest
from callbox.audio import to_wav
from callbox.errors import AppError
from callbox.providers import NO_SPEECH, OpenAIProvider, OpenRouterProvider, build_provider


@pytest.fixture
def orconfig(config):
    config.provider_kind = 'openrouter'
    config.openrouter_key = 'unit-test-not-a-real-key'
    return config


def provider(config, handler):
    return OpenRouterProvider(config, httpx.MockTransport(handler))


def speech_wav(seconds=1):
    """Audible audio: loud enough to pass the local silence gate, so tests
    below exercise the provider contract rather than the gate."""
    import math
    import struct
    pcm = b''.join(struct.pack('<h', int(6000 * math.sin(2 * math.pi * 440 * t / 16000)))
                   for t in range(int(16000 * seconds)))
    return to_wav(pcm)


def sse(events):
    body = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'
    return httpx.Response(200, content=body.encode(), headers={'content-type': 'text/event-stream'})


def audio_delta(pcm):
    return {'choices': [{'delta': {'audio': {'data': base64.b64encode(pcm).decode()}}}]}


def completion(content, cost=0.0001):
    return {'choices': [{'message': {'content': content}}], 'usage': {'cost': cost}}


def transcript(text, present=True, cost=0.0001):
    """Transcription now returns a structured object, not bare text."""
    return completion(json.dumps({'transcript': text, 'speech_present': present}), cost)


# --- selection -----------------------------------------------------------

def test_build_provider_honours_the_configured_kind(config):
    config.provider_kind = 'openrouter'
    assert isinstance(build_provider(config), OpenRouterProvider)
    config.provider_kind = 'openai'
    assert isinstance(build_provider(config), OpenAIProvider)


def test_provider_configured_reads_the_matching_key(config):
    config.provider_kind = 'openrouter'
    config.openrouter_key = ''
    config.api_key = 'an-openai-key'
    assert not config.provider_configured, 'an OpenAI key must not enable the OpenRouter path'
    config.openrouter_key = 'an-openrouter-key'
    assert config.provider_configured


def test_api_key_is_required(orconfig):
    orconfig.openrouter_key = ''
    with pytest.raises(AppError) as e:
        asyncio.run(OpenRouterProvider(orconfig).classify('test', '2026-09-14'))
    assert e.value.code == 'provider_not_configured'


# --- transcription -------------------------------------------------------

def test_transcription_uses_chat_completions_with_an_audio_part(orconfig):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        assert request.url.path == '/api/v1/chat/completions'
        return httpx.Response(200, json=transcript('Book tomorrow'))

    text = asyncio.run(provider(orconfig, handle).transcribe(speech_wav(), 'audio/wav'))
    assert text == 'Book tomorrow'
    body = seen[0]
    assert body['model'] == orconfig.openrouter_stt_model
    assert body['reasoning'] == {'effort': 'low'}, 'reasoning tokens dominated measured cost'
    assert body['response_format']['json_schema']['strict'] is True, 'extraction, not conversation'
    parts = body['messages'][-1]['content']
    assert parts[1]['type'] == 'input_audio' and parts[1]['input_audio']['format'] == 'wav'


def test_silence_fails_closed_instead_of_confabulating(orconfig):
    """A chat model asked to transcribe a tone returns invented words, so the
    empty-string check alone can never fire. The sentinel must."""
    p = provider(orconfig, lambda r: httpx.Response(200, json=transcript('', present=False)))
    with pytest.raises(AppError) as e:
        asyncio.run(p.transcribe(speech_wav(), 'audio/wav'))
    assert e.value.code == 'no_speech'


def test_blank_transcript_still_fails_closed(orconfig):
    p = provider(orconfig, lambda r: httpx.Response(200, json=transcript('   ')))
    with pytest.raises(AppError) as e:
        asyncio.run(p.transcribe(speech_wav(), 'audio/wav'))
    assert e.value.code == 'no_speech'


def test_overlong_transcript_rejected(orconfig):
    p = provider(orconfig, lambda r: httpx.Response(200, json=transcript('x' * 2100)))
    with pytest.raises(AppError) as e:
        asyncio.run(p.transcribe(speech_wav(), 'audio/wav'))
    assert e.value.code == 'transcript_too_long'


# --- intent --------------------------------------------------------------

def test_structured_intent_contract(orconfig):
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body['response_format']['json_schema']['strict'] is True
        return httpx.Response(200, json=completion('{"intent":"book","date":"2026-09-15"}'))

    result = asyncio.run(provider(orconfig, handle).classify('Can I schedule a visit?', '2026-09-14'))
    assert result['intent'] == 'book' and result['date'] == '2026-09-15'
    assert '2026-09-14' in seen[0]['messages'][0]['content']


def test_invalid_intent_fails_closed(orconfig):
    p = provider(orconfig, lambda r: httpx.Response(200, json=completion('{"intent":"prescribe","date":null}')))
    with pytest.raises(AppError) as e:
        asyncio.run(p.classify('test', '2026-09-14'))
    assert e.value.code == 'provider_format'


# --- speech --------------------------------------------------------------

def test_speech_streams_and_returns_a_wav(orconfig):
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        return sse([audio_delta(bytes(640)), audio_delta(bytes(640))])

    out = asyncio.run(provider(orconfig, handle).speak('Your appointment is confirmed.', wav=True))
    assert out.startswith(b'RIFF'), 'callers resample provider WAV; raw PCM would break them'
    assert seen[0]['stream'] is True, 'OpenRouter refuses audio output without stream:true'
    assert seen[0]['audio']['format'] == 'pcm16'
    assert seen[0]['modalities'] == ['text', 'audio']
    # The reply text must be delimited inside one instruction turn. Passed as a
    # bare user message, a conversational audio model answers it instead of
    # voicing it, so the caller hears a different sentence than the one the
    # agent committed to the transcript.
    sent = seen[0]['messages']
    assert len(sent) == 1 and sent[0]['role'] == 'user'
    assert '<speak>Your appointment is confirmed.</speak>' in sent[0]['content']
    assert 'word for word' in sent[0]['content']


def test_speech_without_audio_fails_closed(orconfig):
    p = provider(orconfig, lambda r: sse([{'choices': [{'delta': {'content': 'hello'}}]}]))
    with pytest.raises(AppError) as e:
        asyncio.run(p.speak('test'))
    assert e.value.code == 'provider_format'


def test_speech_mime_matches_the_returned_bytes(orconfig):
    assert OpenRouterProvider(orconfig).speech_mime == 'audio/wav'
    assert OpenAIProvider(orconfig).speech_mime == 'audio/mpeg'


# --- failure handling ----------------------------------------------------

@pytest.mark.parametrize('status,code', [(401, 'provider_error'), (429, 'provider_rate_limit'),
                                         (500, 'provider_error')])
def test_provider_failures_sanitized(orconfig, status, code):
    p = provider(orconfig, lambda r: httpx.Response(status, json={'secret': 'key-and-private-text'}))
    with pytest.raises(AppError) as e:
        asyncio.run(p.classify('test', '2026-09-14'))
    assert e.value.code == code and 'key-and-private-text' not in str(e.value)


def test_timeout_is_reported_without_retrying(orconfig):
    def handle(request):
        raise httpx.TimeoutException('slow', request=request)

    with pytest.raises(AppError) as e:
        asyncio.run(provider(orconfig, handle).classify('test', '2026-09-14'))
    assert e.value.code == 'provider_timeout'


# --- cost ceiling --------------------------------------------------------

def test_turn_cost_ceiling_rejects_an_expensive_turn(orconfig):
    orconfig.max_turn_cost_usd = 0.01
    p = provider(orconfig, lambda r: httpx.Response(200, json=completion('{"intent":"book","date":null}', cost=0.5)))
    with pytest.raises(AppError) as e:
        asyncio.run(p.classify('test', '2026-09-14'))
    assert e.value.code == 'provider_cost' and '0.5' in str(e.value)


def test_turn_cost_recorded_when_within_budget(orconfig):
    orconfig.max_turn_cost_usd = 1.0
    p = provider(orconfig, lambda r: httpx.Response(200, json=completion('{"intent":"book","date":null}', cost=0.004)))
    asyncio.run(p.classify('test', '2026-09-14'))
    assert p.last_cost_usd == pytest.approx(0.004)


# --- F0: silence must fail closed without reaching the provider ----------

def test_silence_is_refused_before_any_paid_request(orconfig):
    """Measured: asked to transcribe one second of digital silence, every
    model tried returned a confident invented sentence rather than the
    NO_SPEECH sentinel. The gate cannot depend on model compliance."""
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=transcript('The company headquarter is in Washington DC.'))

    p = provider(orconfig, handle)
    with pytest.raises(AppError) as e:
        asyncio.run(p.transcribe(to_wav(bytes(16000 * 2)), 'audio/wav'))
    assert e.value.code == 'no_speech'
    assert calls == [], 'silent audio must not be sent upstream at all'


def test_audible_speech_passes_the_silence_gate(orconfig):
    p = provider(orconfig, lambda r: httpx.Response(200, json=transcript('book tomorrow')))
    assert asyncio.run(p.transcribe(speech_wav(), 'audio/wav')) == 'book tomorrow'


def test_reasoning_effort_is_low_not_disabled(orconfig):
    """Gemini rejects reasoning:{enabled:false} with 'Reasoning is mandatory
    for this endpoint and cannot be disabled.'"""
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"intent":"hours","date":null}'))

    asyncio.run(provider(orconfig, handle).classify('when do you open', '2026-09-14'))
    assert seen[0]['reasoning'] == {'effort': 'low'}


def test_silence_gate_thresholds():
    import math
    import struct
    from callbox.audio import SILENCE_RMS, pcm_rms
    silence = bytes(16000 * 2)
    speech = b''.join(struct.pack('<h', int(6000 * math.sin(2 * math.pi * 440 * t / 16000)))
                      for t in range(16000))
    assert pcm_rms(silence) == 0
    assert pcm_rms(speech) > SILENCE_RMS * 10
    assert pcm_rms(b'') == 0
