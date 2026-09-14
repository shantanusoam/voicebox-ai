from fastapi.testclient import TestClient
import pytest
from callbox.db import digest
from callbox.main import create_app


def test_auth_required(app):
    with TestClient(app) as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/api/calls').status_code==401
        assert client.get('/api/workspace').status_code==401

def test_bad_login(app):
    with TestClient(app) as client:
        assert client.post('/api/auth/login',json={'token':'not-the-right-token-0000'}).status_code==401

def test_login_cookie_attributes(app,config):
    with TestClient(app) as client:
        r=client.post('/api/auth/login',json={'token':config.admin_token})
        cookie=r.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie
        assert config.admin_token not in cookie
        assert client.get('/api/calls').status_code==200
        assert client.post('/api/auth/logout').status_code==200
        assert client.get('/api/calls').status_code==401

def test_cross_origin_write_blocked(client):
    r=client.post('/api/calls',json={'label':'test'},headers={'origin':'https://attacker.invalid'})
    assert r.status_code==403

def test_dns_rebinding_host_blocked(client):
    assert client.get('/api/workspace',headers={'host':'attacker.invalid'}).status_code==403

def test_private_data_not_in_config(client,config):
    config.api_key='secret-provider-key'
    text=client.get('/api/workspace').text
    assert 'secret-provider-key' not in text and config.admin_token not in text

def test_sensitive_response_headers(client):
    r=client.get('/api/calls')
    assert r.headers['cache-control']=='no-store'
    assert r.headers['x-content-type-options']=='nosniff'
    r=client.get('/app/')
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert "script-src 'self'" in r.headers['content-security-policy']

def test_device_token_hashed_and_once_only(client,db):
    r=client.post('/api/devices',json={'name':'Test device','kind':'simulator'}).json()
    assert r['token'] not in client.get('/api/devices').text
    raw=db.one('SELECT token_hash FROM devices WHERE id=?',(r['id'],))
    assert raw['token_hash']==digest(r['token']) and raw['token_hash']!=r['token']

def test_device_revoke(client,db):
    d=client.post('/api/devices',json={'name':'Test device','kind':'simulator'}).json()
    assert db.authenticate_device(d['token'],d['id'])
    assert client.post('/api/devices/'+d['id']+'/revoke',json={'confirm':True}).status_code==200
    assert db.authenticate_device(d['token'],d['id']) is None

def test_workspace_scoping(client,db):
    db.create_workspace('another-workspace')
    c=db.new_call('another-workspace','Private different tenant')
    d=db.create_device('another-workspace','Other device','simulator')
    assert client.get('/api/calls/'+c['id']).status_code==404
    assert client.get('/api/calls/'+c['id']+'/export').status_code==404
    assert client.post('/api/devices/'+d['id']+'/revoke',json={'confirm':True}).status_code==404
    assert 'Private different tenant' not in client.get('/api/calls').text

@pytest.mark.parametrize('body',[
 {'label':'A','workspace':'someone-else'}, {'label':'A','source':'pstn'}, {'label':'','language':'en'},
 {'label':'A','provider':'arbitrary'}, {'label':'A'*81}])
def test_invalid_call_payload(client,body):assert client.post('/api/calls',json=body).status_code==422

@pytest.mark.parametrize('body',[
 {'text':'','request_id':'12345678'}, {'text':'x'*2001,'request_id':'12345678'},
 {'text':'hello','request_id':'short'}, {'text':'hello','request_id':'bad / key'},
 {'text':'hello','request_id':'12345678','workspace':'another'}])
def test_invalid_turn_payload(client,make_call,body):
    assert client.post('/api/calls/'+make_call()['id']+'/turn',json=body).status_code==422

def test_provider_disabled_by_default(client):
    r=client.post('/api/calls',json={'label':'test','provider':'openai','consent':True})
    assert r.status_code==503

def test_provider_requires_explicit_consent(client,config):
    config.api_key='synthetic-test-key'
    assert client.post('/api/calls',json={'label':'test','provider':'openai'}).status_code==422

def test_no_audio_processing_on_local_session(client,make_call):
    r=client.post('/api/calls/'+make_call()['id']+'/audio?request_id=12345678',content=b'x'*100,headers={'content-type':'audio/webm'})
    assert r.status_code==403

def test_sql_input_is_not_query_code(client,make_call):
    make_call(label="Robert'); DROP TABLE calls;--")
    assert client.get('/api/calls').status_code==200
    assert len(client.get('/api/calls').json())==1

def test_no_runtime_files_served(client):
    for path in ['/.env','/.runtime/admin-token','/.runtime/callbox.db','/app/../.runtime/admin-token']:
        assert client.get(path).status_code==404

def test_demo_login_disabled_outside_demo(config,db):
    config.demo=False
    with TestClient(create_app(config,db)) as client:
        assert client.post('/api/auth/demo').status_code==403
