import weakref
from contextlib import asynccontextmanager
from pathlib import Path
import base64
import asyncio
import re
import contextlib
import hashlib
import json
import secrets
import time
from urllib.parse import urlparse
from fastapi import FastAPI, Request, WebSocket, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from .config import Config, ROOT
from .db import Database, digest
from .errors import AppError
from .models import Login, NewCall, Turn, DeviceCreate, Settings, Confirmation
from .providers import OpenAIProvider
from .agent import Agent
from .gateway import Gateway
from .limits import JsonBodyLimit


def create_app(config=None, db=None, provider=None):
    config = config or Config.from_env()
    own_db = db is None
    db = db or Database(config.data_dir / 'callbox.db', config.workspace, config.demo)
    provider = provider or OpenAIProvider(config)
    agent = Agent(db, config, provider)
    gateway = Gateway(db, agent, provider, config)
    rate_buckets = {}
    audio_locks = weakref.WeakValueDictionary()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for socket in list(gateway.connections.values()):
            with contextlib.suppress(Exception): await socket.close(1001)
        if own_db: db.close()

    app = FastAPI(title='CallBox Lab API', version='0.2.0', lifespan=lifespan,
                  description='Local development API. Not a production phone/medical service.')
    app.state.db, app.state.agent, app.state.gateway = db, agent, gateway
    app.state.config, app.state.provider = config, provider
    app.add_middleware(JsonBodyLimit)

    @app.exception_handler(AppError)
    async def handle_error(request, error):
        return JSONResponse({'error':{'code':error.code,'message':error.message}}, status_code=error.status)

    @app.middleware('http')
    async def security(request: Request, call_next):
        host = request.url.hostname
        # Local lab rejects arbitrary Host values, including DNS rebinding.
        import os
        public_origin = os.getenv('CALLBOX_PUBLIC_ORIGIN','')
        public_host = urlparse(public_origin).hostname
        allowed = {'localhost','127.0.0.1','::1','testserver'}
        if public_host: allowed.add(public_host)
        if host not in allowed:
            return JSONResponse({'error':{'code':'host','message':'Host not permitted for this lab.'}}, status_code=403)
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin = request.headers.get('origin')
            expected = public_origin or str(request.base_url).rstrip('/')
            if origin and origin.rstrip('/') != expected:
                return JSONResponse({'error':{'code':'origin','message':'Cross-origin writes are not allowed.'}},status_code=403)
        path = request.url.path
        if path.startswith('/api/'):
            ip = request.client.host if request.client else 'unknown'
            bucket = (ip, 'auth' if path.startswith('/api/auth/') else 'api')
            limit = 30 if bucket[1]=='auth' else 600
            window = int(time.time()//60)
            previous_window, count = rate_buckets.get(bucket, (window,0))
            count = count+1 if previous_window==window else 1
            rate_buckets[bucket] = (window,count)
            # Hard cap prevents attacker-controlled source addresses growing memory indefinitely.
            if len(rate_buckets)>2000: rate_buckets.clear()
            if count>limit:
                return JSONResponse({'error':{'code':'rate_limit','message':'Too many requests. Please pause before retrying.'}}, status_code=429)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Permissions-Policy'] = 'microphone=(self), camera=(), geolocation=()'
        if path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        if path.startswith('/app'):
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    def workspace(request: Request):
        session = db.authenticate_session(request.cookies.get('callbox_session'))
        if session: return session['workspace']
        authorization = request.headers.get('authorization','')
        if authorization.startswith('Bearer ') and secrets.compare_digest(authorization[7:].encode(), config.admin_token.encode()):
            return config.workspace
        raise AppError(401,'auth_required','Sign in to the local CallBox workspace.')

    def login_response():
        token = db.login(config.workspace, config.session_seconds)
        response = JSONResponse({'ok':True})
        response.set_cookie('callbox_session',token,httponly=True,samesite='strict',secure=config.secure_cookie,
                            max_age=config.session_seconds,path='/')
        return response

    @app.get('/healthz')
    async def health(): return {'ok':True,'version':'0.2.0','mode':'development-lab','hardware_verified':False}

    @app.get('/api/bootstrap')
    async def bootstrap(request: Request):
        local = request.client and request.client.host in {'127.0.0.1','::1','testclient'}
        return {'demo_login':bool(config.demo and local),'api_version':'0.2.0'}

    @app.post('/api/auth/login')
    async def login(body: Login):
        if not secrets.compare_digest(body.token.encode(),config.admin_token.encode()):
            raise AppError(401,'invalid_token','Invalid workspace token.')
        return login_response()

    @app.post('/api/auth/demo')
    async def demo_login(request: Request):
        if not config.demo or not request.client or request.client.host not in {'127.0.0.1','::1','testclient'}:
            raise AppError(403,'demo_disabled','Local demo login is not available.')
        return login_response()

    @app.post('/api/auth/logout')
    async def logout(request: Request, ws=Depends(workspace)):
        token = request.cookies.get('callbox_session','')
        db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(token),))
        response=JSONResponse({'ok':True});response.delete_cookie('callbox_session',path='/')
        return response

    @app.get('/api/workspace')
    async def info(ws=Depends(workspace)):
        return {'id':ws,'settings':db.settings(ws),'demo':config.demo,'provider_configured':bool(config.api_key),
                'provider_models':{'stt':config.stt_model,'tts':config.tts_model,'intent':config.intent_model},
                'calendar':'local-sqlite','telephony':'not-connected','hardware_verified':False}

    @app.get('/api/summary')
    async def summary(ws=Depends(workspace)): return db.summary(ws)

    @app.get('/api/calls')
    async def calls(q: str='', ws=Depends(workspace)):
        return db.calls(ws,q[:80])

    @app.post('/api/calls',status_code=201)
    async def new_call(body: NewCall,ws=Depends(workspace)):
        return agent.start(ws,**body.model_dump())

    @app.get('/api/calls/{cid}')
    async def get_call(cid: str,ws=Depends(workspace)): return db.call(ws,cid)

    @app.post('/api/calls/{cid}/turn')
    async def turn(cid: str,body: Turn,ws=Depends(workspace)):
        return await agent.turn(ws,cid,body.text,body.request_id)

    @app.post('/api/calls/{cid}/end')
    async def end_call(cid: str,ws=Depends(workspace)):
        async with agent.lock(cid): result=db.end_call(ws,cid)
        agent.locks.pop(cid,None)
        audio_locks.pop(cid,None)
        return result

    @app.get('/api/calls/{cid}/export')
    async def export_call(cid: str,ws=Depends(workspace)):
        call=db.call(ws,cid)
        return JSONResponse({'version':'callbox.export.v1','call':call,'scope':'Local development record, not a medical chart.'},
                            headers={'Content-Disposition':f'attachment; filename="{cid}.json"'})

    @app.post('/api/calls/{cid}/audio')
    async def audio_turn(cid: str,request: Request,request_id: str,ws=Depends(workspace)):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise AppError(422,'request_id','Provide a valid unique request ID before sending audio.')
        call=db.call(ws,cid)
        if call['provider']!='openai' or not call['consent']:
            raise AppError(403,'voice_not_enabled','Create an OpenAI test session with explicit consent first.')
        if call['status']!='active': raise AppError(409,'call_ended','This session has ended.')
        mime=request.headers.get('content-type','').split(';')[0]
        audio=bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio)>4*1024*1024: raise AppError(413,'audio_size','Maximum recording size is 4 MB.')
        payload=hashlib.sha256(audio).hexdigest()
        # Serialize paid uploads per call. A duplicate racing request cannot
        # both miss the cache and start a second billed transcription.
        async with audio_locks.setdefault(cid,asyncio.Lock()):
            cached=db.cached(ws,'audio:'+cid,request_id,payload)
            if cached: return {**cached,'replayed':True}
            if db.call(ws,cid)['status']!='active':
                raise AppError(409,'call_ended','This session has ended.')
            text=await provider.transcribe(bytes(audio),mime)
            result=await agent.turn(ws,cid,text,request_id)
            response={**result,'transcript':text}
            # Persist result BEFORE TTS. Interruption/cancellation after a
            # booking cannot make a later retry repeat that side effect.
            db.cache(ws,'audio:'+cid,request_id,payload,response)
            try:
                speech=await provider.speak(result['reply'])
                response['audio_base64']=base64.b64encode(speech).decode()
                response['audio_mime']='audio/mpeg'
            except AppError as error:
                response['speech_error']=error.message
            # No input audio or generated speech is written to disk.
            return response

    @app.get('/api/availability')
    async def availability(date: str,ws=Depends(workspace)): return db.slots(ws,date)

    @app.get('/api/appointments')
    async def appointments(ws=Depends(workspace)): return db.appointments(ws)

    @app.post('/api/appointments/{aid}/cancel')
    async def cancel(aid: str,body: Confirmation,ws=Depends(workspace)): return db.cancel_appointment(ws,aid)

    @app.get('/api/tasks')
    async def tasks(ws=Depends(workspace)): return db.tasks(ws)

    @app.post('/api/tasks/{tid}/resolve')
    async def resolve(tid: str,body: Confirmation,ws=Depends(workspace)): return db.resolve_task(ws,tid)

    @app.get('/api/devices')
    async def devices(ws=Depends(workspace)): return db.devices(ws)

    @app.post('/api/devices',status_code=201)
    async def create_device(body: DeviceCreate,ws=Depends(workspace)): return db.create_device(ws,body.name,body.kind)

    @app.post('/api/devices/{did}/revoke')
    async def revoke(did: str,body: Confirmation,ws=Depends(workspace)):
        result=db.revoke_device(ws,did)
        await gateway.revoke(did)
        return result

    @app.get('/api/events')
    async def events(after: int=0,ws=Depends(workspace)): return db.events(ws,max(after,0))

    @app.put('/api/settings')
    async def update_settings(body: Settings,ws=Depends(workspace)):
        # Changing duration while appointments exist could create overlapping slots.
        previous=db.settings(ws);new=body.model_dump()
        live=db.one("SELECT COUNT(*) n FROM calls WHERE workspace=? AND status='active'",(ws,))['n']
        schedule_keys=('open_hour','close_hour','slot_minutes')
        schedule_changed=any(previous[k]!=new[k] for k in schedule_keys)
        if schedule_changed and (live or any(a['status']=='confirmed' for a in db.appointments(ws))):
            raise AppError(409,'schedule_in_use','End active tests and clear confirmed local appointments before changing the schedule.')
        return db.set_settings(ws,new)

    @app.websocket('/ws/device')
    async def device_socket(socket: WebSocket):
        origin=socket.headers.get('origin')
        expected=str(socket.url).replace('ws://','http://').replace('wss://','https://').split('/ws/')[0]
        if origin and origin.rstrip('/')!=expected:
            await socket.close(1008);return
        await gateway.handle(socket)

    @app.get('/app')
    async def app_redirect(): return RedirectResponse('/app/')

    app.mount('/app',StaticFiles(directory=ROOT/'web'/'app',html=True),name='console')
    app.mount('/',StaticFiles(directory=ROOT/'web'/'site',html=True),name='website')
    return app
