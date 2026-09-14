import asyncio
import base64
import json
from fastapi.testclient import TestClient
import httpx
import pytest
from callbox.main import create_app
from callbox.db import digest


def test_oversized_json_is_rejected_before_validation(client):
    r=client.post('/api/calls',content=json.dumps({'label':'x'*70000}),headers={'content-type':'application/json'})
    assert r.status_code==413 and r.json()['error']['code']=='body_size'

def test_malformed_device_id_does_not_raise_internal_error(client):
    with client.websocket_connect('/ws/device') as ws:
        ws.send_json({'type':'hello','protocol':'callbox.v1','device_id':{'invalid':'type'},'token':'bad'})
        assert ws.receive_json()['code']=='device_auth'

def test_audio_request_id_validated_before_provider(client,make_call,config):
    config.api_key='not-a-real-key';c=make_call(provider='openai',consent=True)
    r=client.post('/api/calls/'+c['id']+'/audio?request_id=bad%20key',content=b'x'*100,headers={'content-type':'audio/webm'})
    assert r.status_code==422

class FakeProvider:
    def __init__(self):self.transcriptions=0;self.speech=0
    async def transcribe(self,audio,mime):
        self.transcriptions+=1
        await asyncio.sleep(.04)
        return 'What are your hours?'
    async def speak(self,text,wav=False):self.speech+=1;return b'test mp3 bytes'
    async def classify(self,text,today):return {'intent':'hours','date':None}

def test_concurrent_duplicate_audio_not_transcribed_twice(config,db):
    config.api_key='fake-key';p=FakeProvider();app=create_app(config,db,p)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',headers={'Authorization':'Bearer '+config.admin_token}) as client:
            c=(await client.post('/api/calls',json={'label':'Speech lab','provider':'openai','consent':True})).json()
            url='/api/calls/'+c['id']+'/audio?request_id=concurrent-audio-0001'
            replies=await asyncio.gather(*(client.post(url,content=b'x'*100,headers={'Content-Type':'audio/webm'}) for _ in range(2)))
            assert all(r.status_code==200 for r in replies)
            assert p.transcriptions==1 and p.speech==1
            assert sorted(r.json()['replayed'] for r in replies)==[False,True]
    asyncio.run(run())

def test_audio_hash_conflict_prevents_second_paid_call(config,db):
    config.api_key='fake-key';p=FakeProvider()
    with TestClient(create_app(config,db,p)) as client:
        client.post('/api/auth/demo');c=client.post('/api/calls',json={'label':'Speech lab','provider':'openai','consent':True}).json()
        url='/api/calls/'+c['id']+'/audio?request_id=voice-repeat-test-01'
        a=client.post(url,content=b'x'*100,headers={'content-type':'audio/webm'})
        b=client.post(url,content=b'y'*100,headers={'content-type':'audio/webm'})
        assert a.status_code==200 and b.status_code==409
        assert p.transcriptions==1

def test_call_turn_budget(client,make_call,db):
    cid=make_call()['id']
    for i in range(160):db.message(cid,'caller' if i%2==0 else 'assistant','synthetic')
    r=client.post('/api/calls/'+cid+'/turn',json={'text':'hours','request_id':'turn-limit-test-01'})
    assert r.status_code==429 and r.json()['error']['code']=='turn_limit'

def test_live_device_revocation_disconnects(client):
    d=client.post('/api/devices',json={'name':'Live revoke','kind':'simulator'}).json()
    with client.websocket_connect('/ws/device') as ws:
        ws.send_json({'type':'hello','protocol':'callbox.v1','device_id':d['id'],'token':d['token']})
        assert ws.receive_json()['type']=='ready'
        client.post('/api/devices/'+d['id']+'/revoke',json={'confirm':True})
        assert ws.receive()['type']=='websocket.close'

def test_nonascii_login_token_rejected_without_internal_error(app):
    with TestClient(app) as client:
        r=client.post('/api/auth/login',json={'token':'\u00e9'*40})
        assert r.status_code==401 and r.json()['error']['code']=='invalid_token'

def test_idle_agent_locks_do_not_accumulate(app,client,make_call):
    import gc
    c=make_call()
    assert client.post('/api/calls/'+c['id']+'/turn',json={'text':'hours','request_id':'weak-lock-test-001'}).status_code==200
    gc.collect()
    assert len(app.state.agent.locks)==0

def test_prune_is_dry_run_until_explicitly_applied(tmp_path):
    import os,subprocess,sys,sqlite3
    from pathlib import Path
    from callbox.db import Database
    root=Path(__file__).resolve().parents[1]
    db=Database(tmp_path/'callbox.db',seed=False)
    call=db.new_call('clinic-demo',label='Old synthetic caller',language='en',source='browser',provider='local',consent=False)
    db.message(call['id'],'caller','synthetic old text')
    db.end_call('clinic-demo',call['id'])
    db.execute('UPDATE calls SET started_at=? WHERE id=?',('2000-01-01T00:00:00+00:00',call['id']))
    db.close()
    env={**os.environ,'CALLBOX_DATA_DIR':str(tmp_path),'CALLBOX_ADMIN_TOKEN':'prune-test-token-only-00000000000000000','OPENAI_API_KEY':''}
    command=[sys.executable,str(root/'scripts/prune.py'),'--days','30']
    dry=subprocess.run(command,env=env,capture_output=True,text=True,check=True)
    assert json.loads(dry.stdout)['dry_run'] is True
    con=sqlite3.connect(tmp_path/'callbox.db')
    assert con.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==1
    applied=subprocess.run(command+['--apply'],env=env,capture_output=True,text=True,check=True)
    assert json.loads(applied.stdout)['ended_calls']==1
    assert con.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==0
    assert con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0
    con.close()
