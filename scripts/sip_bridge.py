#!/usr/bin/env python3
"""Answer SIP calls with the CallBox realtime agent, via Asterisk AudioSocket.

    python scripts/sip_bridge.py --port 8090

Asterisk dials AudioSocket(<uuid>,host:8090); this process accepts that TCP
connection, translates 8 kHz telephony audio to the 24 kHz the Realtime API
requires, and runs the same engine, tools and booking guarantees the browser
console uses. No telephony credentials are needed: a softphone registered to
the local Asterisk is enough to prove the whole path.

Multi-tenant routing: AudioSocket carries no dialled number, so the dialplan
registers the call first (uuid -> called/caller) over HTTP, and the number
decides which tenant answers. An unrecognised number is refused rather than
sent to a default tenant: answering a stranger's call with someone else's
business identity, and writing to their database, is worse than not answering.

This does NOT make it a phone service. There is no carrier, no DID and no
PSTN connectivity here; a real number needs a licensed SIP trunk.
"""
import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from callbox import realtime                      # noqa: E402
from callbox.agent import Agent                   # noqa: E402
from callbox.config import Config                 # noqa: E402
from callbox.db import Database                   # noqa: E402
from callbox.errors import AppError               # noqa: E402
from callbox.providers import build_provider      # noqa: E402
from callbox.orchestrator import Orchestrator     # noqa: E402
from callbox.tenancy import NumberService         # noqa: E402
from callbox.telephony import AudioSocketLeg      # noqa: E402


class Bridge:
    def __init__(self, config):
        self.config = config
        self.db = Database(config.data_dir / 'callbox.db', config.workspace, config.demo)
        self.agent = Agent(self.db, config, build_provider(config))
        # One registry shared by every call; per-call state lives in ToolContext.
        self.orchestrator = Orchestrator(self.db, config)
        self.numbers = NumberService(self.db)
        self.pending = {}          # audiosocket uuid -> {called, caller}
        self.calls = 0

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')
        self.calls += 1
        index = self.calls
        digits = []
        leg = AudioSocketLeg(reader, writer, on_dtmf=digits.append)
        print(f'[call {index}] connected from {peer}', flush=True)
        call = None
        workspace = None
        try:
            await leg.await_uuid()
            routing = self.pending.pop(leg.call_uuid, None) if leg.call_uuid else None
            called = (routing or {}).get('called')
            workspace = self.numbers.resolve(called) if called else None
            if workspace is None and not self.numbers.numbers():
                # No numbers provisioned at all: single-tenant lab mode.
                workspace = self.config.workspace
            if workspace is None:
                print(f'[call {index}] refused: {called!r} is not routed to an active tenant',
                      flush=True)
                return
            settings = self.db.settings(workspace)
            print(f'[call {index}] {called or "lab"} -> tenant {workspace} '
                  f'({settings.get("name")})', flush=True)
            call = self.agent.start(workspace, label=f'SIP caller {index}',
                                    language=settings.get('language', 'en'),
                                    source='sip', provider='openai', consent=True)
            greeting = call['messages'][0]['text']
            print(f'[call {index}] call_id={call["id"]}', flush=True)
            tools = await realtime.run(leg, self.db, self.agent, self.config,
                                       workspace, call['id'], greeting,
                                       channel='sip', orchestrator=self.orchestrator)
            print(f'[call {index}] tools used: {[t["tool"] for t in tools]}', flush=True)
        except AppError as error:
            print(f'[call {index}] {error.code}: {error.message}', flush=True)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            print(f'[call {index}] caller hung up', flush=True)
        except Exception as error:
            print(f'[call {index}] failed: {type(error).__name__}: {error}', flush=True)
        finally:
            if digits:
                print(f'[call {index}] DTMF: {"".join(digits)}', flush=True)
            if call and workspace:
                with contextlib.suppress(Exception):
                    self.db.end_call(workspace, call['id'], 'SIP call ended')
            with contextlib.suppress(Exception):
                await leg.hangup()
            print(f'[call {index}] closed', flush=True)


async def registrar(bridge, host, port):
    """Tiny HTTP listener the dialplan calls before AudioSocket connects.

    GET /register?uuid=...&called=...&caller=...
    AudioSocket has no field for the dialled number, and that number is what
    selects the tenant, so it has to arrive out of band.
    """
    from urllib.parse import parse_qs, urlparse

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            target = line.decode('latin-1').split(' ')[1] if b' ' in line else '/'
            query = parse_qs(urlparse(target).query)
            uuid = (query.get('uuid') or [''])[0].replace('-', '').lower()
            if uuid:
                bridge.pending[uuid] = {'called': (query.get('called') or [''])[0],
                                        'caller': (query.get('caller') or [''])[0]}
                print(f'[route] {uuid[:8]}.. called={bridge.pending[uuid]["called"]!r}', flush=True)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                         b'Connection: close\r\n\r\nok')
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionResetError, IndexError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    return await asyncio.start_server(handle, host, port)


async def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8090)
    args = parser.parse_args()

    config = Config.from_env()
    if not config.realtime_available:
        raise SystemExit('Set OPENAI_API_KEY in .env: the SIP bridge uses the Realtime API.')

    bridge = Bridge(config)
    server = await asyncio.start_server(bridge.handle, args.host, args.port)
    routes = await registrar(bridge, args.host, args.port + 1)
    tenants = bridge.numbers.numbers()
    print(f'CallBox SIP bridge on {args.host}:{args.port}  model={config.realtime_model}')
    print(f'Routing registrar on {args.host}:{args.port + 1}')
    if tenants:
        for n in tenants:
            print(f'  {n["e164"]:<16} -> {n["workspace"]} ({n["status"]})')
    else:
        print(f'  no numbers provisioned; single-tenant lab mode ({config.workspace})')
    print('Asterisk dialplan:  same => n,AudioSocket(${UUID},'
          f'{args.host}:{args.port})')
    print('Synthetic tests only. No carrier, no DID, no PSTN.\n', flush=True)
    async with server, routes:
        await server.serve_forever()


if __name__ == '__main__':
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
