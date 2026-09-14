"""Restricted-environment browser adapter.

The managed Chromium blocks URL navigation, including localhost. Inject the
EXACT console CSS/JS, inline its image, and relay fetch through Python HTTP.
Optional WebSocket shim relays individual request/reply messages to the REAL
local gateway. This tests UI behavior, not native browser networking/CSP/cookies.
Ordinary users should use scripts/browser_smoke.py against localhost instead.
"""
from pathlib import Path
import base64
import json
import httpx
from websockets.sync.client import connect

ROOT=Path(__file__).resolve().parents[1]

BRIDGE=r'''
window.fetch=async function(url,options={}){
 const r=await window.__apiBridge(String(url),{method:options.method||'GET',body:options.body??null,headers:options.headers||{}});
 return new Response(r.body,{status:r.status,headers:r.headers});
};
window.WebSocket=class {
 constructor(url){this.readyState=0;this.id=null;queueMicrotask(async()=>{try{this.id=await window.__wsOpen();this.readyState=1;this.onopen?.({});}catch(e){this.onerror?.(e);this.readyState=3;this.onclose?.({});}});}
 send(data){window.__wsSend(this.id,data).then(data=>{if(data!==null&&this.readyState===1)this.onmessage?.({data});}).catch(e=>{this.onerror?.(e);});}
 close(){if(this.readyState>=2)return;this.readyState=2;window.__wsClose(this.id).finally(()=>{this.readyState=3;this.onclose?.({});});}
};
'''

def setup(page,base='http://127.0.0.1:8787', websocket=True):
    client=httpx.Client(base_url=base,timeout=20)
    sockets={}
    def api(url,options):
        assert url.startswith('/api/'),url
        body=options.get('body')
        r=client.request(options['method'],url,content=body,headers=options.get('headers',{}))
        return {'status':r.status_code,'body':r.text,'headers':{'Content-Type':r.headers.get('content-type','application/json')}}
    def ws_open():
        key=str(len(sockets)+1)
        sockets[key]=connect(base.replace('http','ws',1)+'/ws/device',max_size=4*1024*1024)
        return key
    def ws_send(key,data):
        sock=sockets[key];sock.send(data)
        return sock.recv(timeout=10)
    def ws_close(key):
        if key in sockets:sockets.pop(key).close()
    page.expose_function('__apiBridge',api)
    page.expose_function('__wsOpen',ws_open)
    page.expose_function('__wsSend',ws_send)
    page.expose_function('__wsClose',ws_close)
    html=(ROOT/'web/app/index.html').read_text()
    css=(ROOT/'web/app/styles.css').read_text()
    js=(ROOT/'web/app/app.js').read_text()
    image='data:image/webp;base64,'+base64.b64encode((ROOT/'web/site/assets/callbox-device.webp').read_bytes()).decode()
    logo='data:image/svg+xml;base64,'+base64.b64encode((ROOT/'web/app/brandmark.svg').read_bytes()).decode()
    html=html.replace('<link rel="stylesheet" href="/app/styles.css">','<style>'+css+'</style>')
    html=html.replace('<script src="/app/app.js" type="module"></script>','')
    html=html.replace('/app/brandmark.svg',logo).replace('/assets/callbox-device.webp',image)
    js=js.replace('/assets/callbox-device.webp',image)
    html=html.replace('</body>','<script>'+BRIDGE+'</script><script type="module">'+js+'</script></body>')
    page.set_content(html,wait_until='load')
    return client,sockets
