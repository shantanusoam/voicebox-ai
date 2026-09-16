import asyncio
import base64
from array import array
import io
import json
import math
import wave

import httpx

from callbox.component_providers import GroqSTT, SarvamSTT, SarvamTTS, ToolCallingLLM


def speech_pcm(rate=24000, seconds=0.2):
    samples = array('h', [int(5000 * math.sin(2 * math.pi * 440 * n / rate))
                          for n in range(int(rate * seconds))])
    return samples.tobytes()


def wav_bytes(rate=24000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(speech_pcm(rate))
    return output.getvalue()


def test_groq_stt_uses_dedicated_transcription_endpoint(config):
    config.groq_key = 'test-groq-key'
    seen = {}

    def handler(request):
        seen['url'] = str(request.url)
        seen['body'] = request.content
        assert request.headers['authorization'] == 'Bearer test-groq-key'
        return httpx.Response(200, json={'text': 'kal appointment chahiye'})

    provider = GroqSTT(config, httpx.MockTransport(handler))
    text = asyncio.run(provider.transcribe(speech_pcm(), 24000))
    assert text == 'kal appointment chahiye'
    assert seen['url'].endswith('/openai/v1/audio/transcriptions')
    assert b'whisper-large-v3-turbo' in seen['body']


def test_sarvam_stt_can_request_codemix(config):
    config.sarvam_key = 'test-sarvam-key'
    config.sarvam_stt_model = 'saaras:v3'
    config.sarvam_stt_mode = 'codemix'
    seen = {}

    def handler(request):
        seen['body'] = request.content
        assert request.headers['api-subscription-key'] == 'test-sarvam-key'
        return httpx.Response(200, json={'transcript': 'Dr Sharma kal available hain?'})

    provider = SarvamSTT(config, httpx.MockTransport(handler))
    text = asyncio.run(provider.transcribe(speech_pcm(), 24000, None))
    assert 'Sharma' in text
    assert b'saaras%3Av3' in seen['body'] or b'saaras:v3' in seen['body']
    assert b'codemix' in seen['body']


def test_sarvam_tts_returns_raw_pcm_at_runtime_rate(config):
    config.sarvam_key = 'test-sarvam-key'
    config.sarvam_tts_model = 'bulbul:v3'
    encoded = base64.b64encode(wav_bytes(24000)).decode()
    seen = {}

    def handler(request):
        seen['json'] = json.loads(request.content)
        return httpx.Response(200, json={'audios': [encoded]})

    provider = SarvamTTS(config, httpx.MockTransport(handler))
    pcm = asyncio.run(provider.speak('Namaste, kaise madad karun?', 24000, 'hi-IN'))
    assert len(pcm) == len(speech_pcm())
    assert seen['json']['model'] == 'bulbul:v3'
    assert seen['json']['language_code'] == 'hi-IN'
    assert seen['json']['output_audio_codec'] == 'wav'


def test_tool_llm_normalizes_function_calls_and_cost(config):
    config.pipeline_llm_provider = 'openrouter'
    config.pipeline_llm_model = 'test/model'
    config.openrouter_key = 'test-openrouter-key'
    config.max_turn_cost_usd = 0.05
    seen = {}

    def handler(request):
        seen['json'] = json.loads(request.content)
        return httpx.Response(200, json={
            'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [{
                    'id': 'call_1', 'type': 'function',
                    'function': {'name': 'get_fee', 'arguments': '{}'},
                }],
            }}],
            'usage': {'cost': 0.001},
        })

    llm = ToolCallingLLM(config, httpx.MockTransport(handler))
    result = asyncio.run(llm.complete(
        [{'role': 'system', 'content': 'test'}, {'role': 'user', 'content': 'fee?'}],
        [{'type': 'function', 'name': 'get_fee', 'description': 'fee',
          'parameters': {'type': 'object', 'properties': {}, 'required': []}}]))
    assert result['tool_calls'][0]['name'] == 'get_fee'
    assert result['tool_calls'][0]['arguments'] == {}
    assert result['assistant_message']['tool_calls'][0]['function']['name'] == 'get_fee'
    assert seen['json']['tools'][0]['function']['name'] == 'get_fee'
    assert llm.last_cost_usd == 0.001
