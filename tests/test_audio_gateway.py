import base64
import io
import json
import wave
import pytest
from callbox.audio import AudioBuffer, FRAME_BYTES, MAX_TURN_BYTES, pack_audio_batch, unpack_audio_batch, to_wav, wav_to_pcm16
from callbox.errors import AppError


def frame(seq=0,epoch=0):
    return {'type':'audio','seq':seq,'epoch':epoch,'sample_rate':16000,'pcm16':base64.b64encode(b'\x12\x00'*320).decode()}

def provision(client):return client.post('/api/devices',json={'name':'Protocol test','kind':'simulator'}).json()

def hello(ws,d):
    ws.send_json({'type':'hello','protocol':'callbox.v1','device_id':d['id'],'token':d['token']})
    return ws.receive_json()

def start(ws):
    ws.send_json({'type':'call.start','mode':'echo','consent':True})
    return ws.receive_json()['call_id']

def test_pcm_roundtrip():
    pcm=b'\x12\x00'*320
    assert wav_to_pcm16(to_wav(pcm))==pcm

def test_resample_8k_to16k():
    result=wav_to_pcm16(to_wav(b'\x12\x00'*160,8000))
    assert len(result)==640

def test_invalid_wav():
    with pytest.raises(AppError):wav_to_pcm16(b'not a WAV')


def test_binary_audio_batch_roundtrip():
    frames=[bytes([n])*FRAME_BYTES for n in range(4)]
    payload=pack_audio_batch(frames, first_seq=7, epoch=3)
    decoded=unpack_audio_batch(payload)
    assert decoded['first_seq']==7 and decoded['epoch']==3 and decoded['frames']==frames

def test_binary_audio_batch_rejects_bad_length():
    payload=pack_audio_batch([b'\0'*FRAME_BYTES], first_seq=0)
    with pytest.raises(AppError):unpack_audio_batch(payload[:-1])

def test_audio_interrupt_discards_input_and_increments_epoch():
    b=AudioBuffer();b.add(frame());assert len(b.data)==640
    assert b.interrupt()==1 and len(b.data)==0
    with pytest.raises(AppError):b.add(frame(1,0))
    assert len(b.add(frame(1,1)))==640

@pytest.mark.parametrize('change',[{'sample_rate':8000},{'channels':2},{'seq':-1},{'seq':True},{'seq':1},
                                 {'pcm16':'not base64'},{'pcm16':base64.b64encode(b'abc').decode()},{'epoch':9}])
def test_frame_validation(change):
    data=frame();data.update(change)
    with pytest.raises(AppError):AudioBuffer().add(data)

def test_audio_buffer_hard_limit():
    b=AudioBuffer()
    for n in range(MAX_TURN_BYTES//FRAME_BYTES):b.add(frame(n))
    with pytest.raises(AppError) as e:b.add(frame(MAX_TURN_BYTES//FRAME_BYTES))
    assert e.value.code=='audio_buffer_full'

def test_device_auth_required(client):
    with client.websocket_connect('/ws/device') as ws:
        ws.send_json({'type':'hello','protocol':'callbox.v1','device_id':'fake','token':'bad'})
        assert ws.receive_json()['code']=='device_auth'

def test_protocol_version_required(client):
    with client.websocket_connect('/ws/device') as ws:
        ws.send_json({'type':'hello','protocol':'not-supported'})
        assert ws.receive_json()['code']=='handshake'

def test_gateway_echo_and_interrupt(client,db):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        assert hello(ws,d)['type']=='ready'
        cid=start(ws)
        for n in range(10):
            message={**frame(n),'call_id':cid};ws.send_json(message)
            out=ws.receive_json();assert out['type']=='audio.output' and out['pcm16']==message['pcm16']
        ws.send_json({'type':'interrupt','call_id':cid});out=ws.receive_json()
        assert out['type']=='playback.clear' and out['epoch']==1
        ws.send_json({**frame(10),'call_id':cid});assert ws.receive_json()['code']=='stale_audio'
        ws.send_json({**frame(10,1),'call_id':cid});assert ws.receive_json()['type']=='audio.output'
        ws.send_json({'type':'call.end','call_id':cid});assert ws.receive_json()['type']=='call.ended'
    assert db.call('clinic-demo',cid)['status']=='ended'
    assert db.devices('clinic-demo')[0]['status']=='offline'
    assert db.devices('clinic-demo')[0]['frames']==11

def test_gateway_accepts_binary_audio_batch(client,db):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        assert hello(ws,d)['type']=='ready'
        cid=start(ws)
        frames=[b'\x12\x00'*320 for _ in range(4)]
        ws.send_bytes(pack_audio_batch(frames, first_seq=0, epoch=0))
        for n in range(4):
            out=ws.receive_json()
            assert out['type']=='audio.output' and out['seq']==n
            assert base64.b64decode(out['pcm16'])==frames[n]
        ws.send_json({'type':'call.end','call_id':cid})
        assert ws.receive_json()['type']=='call.ended'
    assert db.devices('clinic-demo')[0]['frames']==4

def test_gateway_rejects_cross_call_frames(client):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        hello(ws,d);start(ws)
        ws.send_json({**frame(),'call_id':'different-call'})
        assert ws.receive_json()['code']=='wrong_call'

def test_gateway_disconnect_finalizes_call(client,db):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:hello(ws,d);cid=start(ws)
    assert db.call('clinic-demo',cid)['outcome']=='Gateway disconnected'

def test_gateway_accepts_text_tools_in_echo_lab(client):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        hello(ws,d);cid=start(ws)
        ws.send_json({'type':'call.text','call_id':cid,'text':'hours','request_id':'gateway-turn-0001'})
        result=ws.receive_json()
        assert result['type']=='turn.result' and '10:00' in result['reply']

def test_only_one_connection_per_device(client):
    d=provision(client)
    with client.websocket_connect('/ws/device') as a:
        hello(a,d)
        with client.websocket_connect('/ws/device') as b:
            assert hello(b,d)['code']=='device_busy'
        a.send_json({'type':'ping'});assert a.receive_json()['type']=='pong'

def test_gateway_revoked_credentials_fail(client):
    d=provision(client);client.post('/api/devices/'+d['id']+'/revoke',json={'confirm':True})
    with client.websocket_connect('/ws/device') as ws:
        assert hello(ws,d)['code']=='device_auth'

def test_gateway_consent_required(client):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        hello(ws,d);ws.send_json({'type':'call.start','mode':'echo'})
        assert ws.receive_json()['code']=='consent_required'

def test_gateway_agent_requires_provider_key(client):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        hello(ws,d);ws.send_json({'type':'call.start','mode':'agent','consent':True})
        assert ws.receive_json()['code']=='provider_not_configured'

def test_gateway_unknown_message(client):
    d=provision(client)
    with client.websocket_connect('/ws/device') as ws:
        hello(ws,d);cid=start(ws);ws.send_json({'type':'dial.outbound','call_id':cid})
        assert ws.receive_json()['code']=='message_type'
