import os
import uvicorn
from .config import Config
from .main import create_app


def main():
    config=Config.from_env()
    host=os.getenv('CALLBOX_HOST','127.0.0.1')
    port=int(os.getenv('CALLBOX_PORT','8787'))
    print('\n  CallBox / development platform v0.2.0')
    print(f'  Website: http://127.0.0.1:{port}/')
    print(f'  Console: http://127.0.0.1:{port}/app/')
    print(f'  API docs: http://127.0.0.1:{port}/docs')
    print(f'  Local admin token file: {config.data_dir / "admin-token"}')
    print('  Synthetic tests only. No real telephone network is connected.\n')
    uvicorn.run(create_app(config),host=host,port=port,ws_max_size=8192,ws_ping_interval=15,
                ws_ping_timeout=20,timeout_keep_alive=5,proxy_headers=False,access_log=False)

if __name__=='__main__': main()
