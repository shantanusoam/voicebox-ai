import asyncio
from datetime import datetime,timedelta
import json
import uuid
import pytest
from callbox.db import IST, Database
from callbox.errors import AppError
from callbox.agent import local_intent, infer_date


def turn(client,cid,text,key=None):
    return client.post(f'/api/calls/{cid}/turn',json={'text':text,'request_id':key or uuid.uuid4().hex})

def reserve(client,call,name='Fictional Alex',confirm=True):
    cid=call['id']
    assert turn(client,cid,'book tomorrow').status_code==200
    assert turn(client,cid,'1').status_code==200
    assert turn(client,cid,name).status_code==200
    return turn(client,cid,'confirm') if confirm else None

@pytest.mark.parametrize('text,intent',[
 ('book tomorrow','book'),('appointment chahiye','book'),('What are your opening hours?','hours'),
 ('fees kitne hain','fee'),('where is the clinic','location'),('I want a human','human'),
 ('Can I change my medicine dose?','medical'),('chest pain','emergency'),('saans nahi aa rahi','emergency'),
 ('reschedule my appointment','human'),('something unclear','unknown'),('report interpretation','medical')])
def test_intent(text,intent): assert local_intent(text)==intent

@pytest.mark.parametrize('phrase,delta',[('today',0),('tomorrow',1),('kal',1),('day after tomorrow',2),('parso',2)])
def test_relative_dates(phrase,delta): assert infer_date(phrase)==str(datetime.now(IST).date()+timedelta(days=delta))

@pytest.mark.parametrize('phrase,needle',[('opening hours','10:00'),('fee','500'),('location','Demo address'),('appointment','Which date')])
def test_faq(client,make_call,phrase,needle):
    result=turn(client,make_call()['id'],phrase)
    assert result.status_code==200
    assert needle in result.json()['reply']

def test_full_booking_requires_confirmation(client,make_call):
    call=make_call();reserve(client,call,confirm=False)
    assert client.get('/api/appointments').json()==[]
    result=turn(client,call['id'],'confirm').json()
    assert result['actions'][0]['status']=='confirmed'
    assert len(client.get('/api/appointments').json())==1
    assert 'No WhatsApp' in result['reply']
    assert result['call']['state']=={}

def test_duplicate_confirmation_is_idempotent(client,make_call):
    call=make_call();reserve(client,call,confirm=False)
    a=turn(client,call['id'],'confirm','same-request-0001')
    b=turn(client,call['id'],'confirm','same-request-0001')
    assert b.json()['replayed'] is True
    assert a.json()['actions']==b.json()['actions']
    assert len(client.get('/api/appointments').json())==1
    assert len(client.get('/api/calls/'+call['id']).json()['messages'])==9

def test_reused_request_with_different_payload_rejected(client,make_call):
    cid=make_call()['id'];turn(client,cid,'hours','same-request-0001')
    response=turn(client,cid,'fee','same-request-0001')
    assert response.status_code==409
    assert response.json()['error']['code']=='idempotency_conflict'

def test_stale_slot_cannot_double_book(client,make_call):
    a,b=make_call(label='Caller A'),make_call(label='Caller B')
    reserve(client,a,confirm=False);reserve(client,b,confirm=False)
    assert 'Confirmed' in turn(client,a['id'],'confirm').json()['reply']
    second=turn(client,b['id'],'confirm').json()
    assert 'just taken' in second['reply']
    assert second['call']['state']['step']=='slot'
    assert len(client.get('/api/appointments').json())==1

def test_cancel_pending_does_not_cancel_existing(client,make_call):
    call=make_call();reserve(client,call)
    result=turn(client,call['id'],'cancel').json()
    assert 'Existing appointments have not been cancelled' in result['reply']
    assert client.get('/api/appointments').json()[0]['status']=='confirmed'

def test_operator_cancellation_frees_slot(client,make_call):
    call=make_call();reserve(client,call)
    apt=client.get('/api/appointments').json()[0]
    assert client.post('/api/appointments/'+apt['id']+'/cancel',json={'confirm':True}).status_code==200
    slots=client.get('/api/availability',params={'date':apt['starts_at'][:10]}).json()
    assert apt['starts_at'] in [s['starts_at'] for s in slots]
    assert client.post('/api/appointments/'+apt['id']+'/cancel',json={'confirm':True}).status_code==200

def test_cancellation_needs_explicit_confirmation(client,make_call):
    call=make_call();reserve(client,call);aid=client.get('/api/appointments').json()[0]['id']
    assert client.post('/api/appointments/'+aid+'/cancel',json={'confirm':False}).status_code==422

@pytest.mark.parametrize('text',['Can I change my dose?','Please interpret my report','I need a human','chest pain','an unrecognized question'])
def test_sensitive_or_unknown_queues_staff(client,make_call,text):
    cid=make_call()['id'];result=turn(client,cid,text).json()
    assert any(a['tool']=='create_staff_request' for a in result['actions'])
    assert len(client.get('/api/tasks').json())==1
    assert not client.get('/api/appointments').json()
    assert 'routine' not in result['reply'].lower()

def test_emergency_stops_booking_flow(client,make_call):
    call=make_call();reserve(client,call,confirm=False)
    r=turn(client,call['id'],'chest pain').json()
    assert 'do not wait' in r['reply']
    assert r['call']['state']=={}
    assert not client.get('/api/appointments').json()

def test_same_call_does_not_duplicate_open_review_tasks(client,make_call):
    cid=make_call()['id'];turn(client,cid,'human');turn(client,cid,'human')
    tasks=client.get('/api/tasks').json();assert len(tasks)==1
    assert client.post('/api/tasks/'+tasks[0]['id']+'/resolve',json={'confirm':True}).status_code==200
    assert client.get('/api/summary').json()['review']==0

@pytest.mark.parametrize('date',['2020-01-01','9999-12-31','2026-02-31','../db','tomorrow',''])
def test_bad_calendar_dates_rejected(client,date):
    assert client.get('/api/availability',params={'date':date}).status_code==422

def test_ended_calls_reject_new_turns(client,make_call):
    cid=make_call()['id'];client.post('/api/calls/'+cid+'/end')
    assert turn(client,cid,'hours').status_code==409
    assert client.post('/api/calls/'+cid+'/end').status_code==200

def test_request_replay_after_call_ends_returns_previous_result(client,make_call):
    cid=make_call()['id'];a=turn(client,cid,'hours','repeatable-request-01').json()
    client.post('/api/calls/'+cid+'/end')
    b=turn(client,cid,'hours','repeatable-request-01').json()
    assert a['reply']==b['reply'] and b['replayed']

def test_schedule_cannot_change_during_active_calls(client,make_call):
    settings=client.get('/api/workspace').json()['settings'];make_call()
    settings['slot_minutes']=15
    assert client.put('/api/settings',json=settings).status_code==409

def test_settings_business_name_can_change(client):
    settings=client.get('/api/workspace').json()['settings'];settings['name']='Fictional Dental Desk'
    assert client.put('/api/settings',json=settings).status_code==200
    assert client.get('/api/workspace').json()['settings']['name']=='Fictional Dental Desk'

def test_invalid_schedule(client):
    settings=client.get('/api/workspace').json()['settings'];settings['close_hour']=8
    assert client.put('/api/settings',json=settings).status_code==422

def test_call_capacity(client,make_call,config):
    config.max_active_calls=2;make_call();make_call()
    assert client.post('/api/calls',json={'label':'Third'}).status_code==429

def test_export_contains_truthful_scope(client,make_call):
    cid=make_call()['id'];result=client.get('/api/calls/'+cid+'/export')
    assert 'attachment' in result.headers['content-disposition']
    assert 'not a medical chart' in result.json()['scope']

def test_hinglish_greeting_and_booking(client,make_call):
    call=make_call(language='hinglish');assert 'Namaste' in call['messages'][0]['text']
    assert 'slots' in turn(client,call['id'],'appointment kal').json()['reply']

def test_persistence_and_restart_recovery(tmp_path):
    file=tmp_path/'persist.db';db=Database(file,seed=False)
    c=db.new_call('clinic-demo','Persisted person');cid=c['id']
    db.message(cid,'caller','hello');db.close()
    second=Database(file,seed=False);stored=second.call('clinic-demo',cid)
    assert stored['status']=='ended' and stored['outcome']=='Server restarted'
    assert stored['messages'][0]['text']=='hello';second.close()
