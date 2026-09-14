import asyncio
import json
import httpx
import pytest
from callbox.providers import OpenAIProvider
from callbox.errors import AppError
from callbox.audio import to_wav


def provider(config,handler):
    config.api_key='unit-test-not-a-real-key'
    return OpenAIProvider(config,httpx.MockTransport(handler))

def test_transcription_request_contract(config):
    seen=[]
    def handle(request):
        seen.append(request)
        assert request.url.path=='/v1/audio/transcriptions'
        assert b'gpt-4o-mini-transcribe' in request.content
        return httpx.Response(200,json={'text':'Book tomorrow'})
    p=provider(config,handle)
    text=asyncio.run(p.transcribe(to_wav(b'\0'*640),'audio/wav'))
    assert text=='Book tomorrow' and len(seen)==1

def test_tts_request_contract(config):
    def handle(request):
        body=json.loads(request.content)
        assert request.url.path=='/v1/audio/speech'
        assert body['response_format']=='wav' and body['model']==config.tts_model
        return httpx.Response(200,content=to_wav(b'\0'*640))
    p=provider(config,handle)
    assert asyncio.run(p.speak('Test response',wav=True)).startswith(b'RIFF')

def test_structured_intent_contract(config):
    def handle(request):
        body=json.loads(request.content)
        assert request.url.path=='/v1/responses'
        assert body['store'] is False
        assert body['text']['format']['strict'] is True
        return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':'{"intent":"book","date":"2026-09-14"}'}]}]})
    p=provider(config,handle)
    assert asyncio.run(p.classify('Can I schedule a visit?','2026-09-13'))['intent']=='book'

@pytest.mark.parametrize('status,code',[(401,'provider_error'),(429,'provider_rate_limit'),(500,'provider_error')])
def test_provider_failures_sanitized(config,status,code):
    p=provider(config,lambda r:httpx.Response(status,json={'secret':'key-and-private-text'}))
    with pytest.raises(AppError) as e:asyncio.run(p.speak('hello'))
    assert e.value.code==code and 'key-and-private-text' not in str(e.value)

def test_invalid_intent_fails_closed(config):
    p=provider(config,lambda r:httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':'{"intent":"prescribe","date":null}'}]}]}))
    with pytest.raises(AppError) as e:asyncio.run(p.classify('test','2026-09-13'))
    assert e.value.code=='provider_format'

def test_blank_transcript(config):
    p=provider(config,lambda r:httpx.Response(200,json={'text':''}))
    with pytest.raises(AppError) as e:asyncio.run(p.transcribe(b'x'*100))
    assert e.value.code=='no_speech'

def test_api_key_is_required(config):
    p=OpenAIProvider(config)
    with pytest.raises(AppError) as e:asyncio.run(p.speak('test'))
    assert e.value.code=='provider_not_configured'
