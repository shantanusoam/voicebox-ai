#!/usr/bin/env python3
"""Start a fresh local API and verify the simulator through native HTTP/WS."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import httpx
ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='callbox-network-') as tmp:
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}';token='native-test-only-'+'a'*40
        env={**os.environ,'CALLBOX_DATA_DIR':tmp,'CALLBOX_ADMIN_TOKEN':token,'CALLBOX_DEMO':'0','CALLBOX_PORT':str(port),'OPENAI_API_KEY':''}
        with open(Path(tmp)/'server.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','callbox'],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:
                        if httpx.get(base+'/healthz',timeout=.5).status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise RuntimeError('Server not ready')
                for path in ['/','/app/','/app/app.js','/app/styles.css','/docs','/openapi.json']:
                    assert httpx.get(base+path).status_code==200,path
                assert httpx.get(base+'/api/calls').status_code==401
                assert httpx.post(base+'/api/auth/demo').status_code==403
                output=ROOT/'qa/native-loopback.json';output.parent.mkdir(exist_ok=True)
                result=subprocess.run([sys.executable,'scripts/device_simulator.py','--url',base,'--admin-token',token,'--frames','100','--output',str(output)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=25)
                if result.returncode:raise RuntimeError(result.stdout+result.stderr)
                print(result.stdout)
                report=json.loads(output.read_text());report.update(http_assets_checked=6,anonymous_api_blocked=True,demo_disabled_tested=True)
                output.write_text(json.dumps(report,indent=2)+'\n')
                schema=httpx.get(base+'/openapi.json').json()
                (ROOT/'docs/openapi.json').write_text(json.dumps(schema,indent=2)+'\n')
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill();server.wait()

if __name__=='__main__':main()
