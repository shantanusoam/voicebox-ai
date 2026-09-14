#!/usr/bin/env python3
"""A real network client for the CallBox lab gateway. No phone calls are placed."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import statistics
import struct
import time
import uuid
import httpx
from websockets.asyncio.client import connect

ROOT=Path(__file__).resolve().parents[1]

async def run(args):
    token=args.admin_token or (ROOT/'.runtime/admin-token').read_text().strip()
    async with httpx.AsyncClient(base_url=args.url,headers={'Authorization':'Bearer '+token},timeout=15) as http:
        response=await http.post('/api/devices',json={'name':'CLI audio simulator','kind':'simulator'})
        response.raise_for_status();device=response.json()
        address=args.url.replace('http://','ws://').replace('https://','wss://')+'/ws/device'
        report={'kind':'native-python-network-loopback','hardware_verified':False,'phone_call_placed':False}
        try:
            async with connect(address,max_size=4*1024*1024) as ws:
                async def exchange(message):
                    await ws.send(json.dumps(message));value=json.loads(await asyncio.wait_for(ws.recv(),8))
                    if value.get('type')=='error':raise RuntimeError(value)
                    return value
                ready=await exchange({'type':'hello','protocol':'callbox.v1','device_id':device['id'],'token':device['token']})
                assert ready['type']=='ready'
                call=await exchange({'type':'call.start','mode':'echo','consent':True});cid=call['call_id']
                latencies=[];matches=0
                for seq in range(args.frames):
                    raw=b''.join(struct.pack('<h',((seq*320+i)%1000)-500) for i in range(320))
                    payload=base64.b64encode(raw).decode();start=time.perf_counter()
                    result=await exchange({'type':'audio','call_id':cid,'seq':seq,'epoch':0,'sample_rate':16000,'pcm16':payload})
                    latencies.append((time.perf_counter()-start)*1000)
                    matches+=int(result.get('pcm16')==payload and result.get('seq')==seq)
                    await asyncio.sleep(.02)
                clear=await exchange({'type':'interrupt','call_id':cid});assert clear['type']=='playback.clear' and clear['epoch']==1
                for text in ['What are your hours?','I would like a human.']:
                    result=await exchange({'type':'call.text','call_id':cid,'text':text,'request_id':uuid.uuid4().hex})
                    assert result['type']=='turn.result'
                end=await exchange({'type':'call.end','call_id':cid});assert end['type']=='call.ended'
                latencies.sort()
                report.update(frames=args.frames,exact_matches=matches,bytes=640*args.frames,
                    p50_ms=round(statistics.median(latencies),3),p95_ms=round(latencies[min(len(latencies)-1,int(len(latencies)*.95))],3),
                    interrupt_acknowledged=True,text_tools_worked=True,call_id=cid)
                assert matches==args.frames
        finally:
            revoked=await http.post('/api/devices/'+device['id']+'/revoke',json={'confirm':True})
            revoked.raise_for_status()
            report['temporary_credentials_revoked']=True
        if args.output:
            Path(args.output).parent.mkdir(parents=True,exist_ok=True)
            Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8787')
    parser.add_argument('--admin-token',default=None)
    parser.add_argument('--frames',type=int,default=100)
    parser.add_argument('--output')
    args=parser.parse_args()
    if not 1<=args.frames<=5000:parser.error('--frames must be between 1 and 5000')
    asyncio.run(run(args))
