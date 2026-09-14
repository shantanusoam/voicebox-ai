#!/usr/bin/env python3
"""Answer SIP calls with the CallBox realtime agent, via Asterisk AudioSocket.

    python scripts/sip_bridge.py --port 8090

Asterisk dials AudioSocket(<uuid>,host:8090); this process accepts that TCP
connection, translates 8 kHz telephony audio to the 24 kHz the Realtime API
requires, and runs the same engine, tools and booking guarantees the browser
console uses. No telephony credentials are needed: a softphone registered to
the local Asterisk is enough to prove the whole path.

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
from callbox.telephony import AudioSocketLeg      # noqa: E402


class Bridge:
    def __init__(self, config):
        self.config = config
        self.db = Database(config.data_dir / 'callbox.db', config.workspace, config.demo)
        self.agent = Agent(self.db, config, build_provider(config))
        # One registry shared by every call; per-call state lives in ToolContext.
        self.orchestrator = Orchestrator(self.db, config)
        self.calls = 0

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')
        self.calls += 1
        index = self.calls
        digits = []
        leg = AudioSocketLeg(reader, writer, on_dtmf=digits.append)
        print(f'[call {index}] connected from {peer}', flush=True)
        call = None
        try:
            call = self.agent.start(self.config.workspace, label=f'SIP caller {index}',
                                    language='en', source='sip', provider='openai', consent=True)
            greeting = call['messages'][0]['text']
            print(f'[call {index}] call_id={call["id"]}', flush=True)
            tools = await realtime.run(leg, self.db, self.agent, self.config,
                                       self.config.workspace, call['id'], greeting,
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
            if call:
                with contextlib.suppress(Exception):
                    self.db.end_call(self.config.workspace, call['id'], 'SIP call ended')
            with contextlib.suppress(Exception):
                await leg.hangup()
            print(f'[call {index}] closed', flush=True)


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
    print(f'CallBox SIP bridge on {args.host}:{args.port}  model={config.realtime_model}')
    print('Asterisk dialplan:  same => n,AudioSocket(${UUID},'
          f'{args.host}:{args.port})')
    print('Synthetic tests only. No carrier, no DID, no PSTN.\n', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
