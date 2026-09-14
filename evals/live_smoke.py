#!/usr/bin/env python3
"""Opt-in live provider smoke test. Makes REAL, BILLED requests.

Never run by the pipeline or CI. It answers the one question the mocked suite
cannot: does the configured provider actually work, and what does a turn cost?

    python evals/live_smoke.py --budget 0.25

Refuses to start without a configured provider, and stops as soon as the
measured spend would exceed --budget.
"""
import argparse
import asyncio
import io
import json
import math
import struct
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from callbox.audio import SILENCE_RMS, wav_rms  # noqa: E402
from callbox.config import Config  # noqa: E402
from callbox.errors import AppError  # noqa: E402
from callbox.providers import build_provider  # noqa: E402

PHRASE = 'Your appointment is confirmed for tomorrow at four thirty.'


def silence_wav(seconds=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(bytes(rate * 2 * seconds))
    return buffer.getvalue()


def tone_wav(seconds=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(b''.join(struct.pack('<h', int(6000 * math.sin(2 * math.pi * 440 * t / rate)))
                                 for t in range(rate * seconds)))
    return buffer.getvalue()


async def run(budget):
    config = Config.from_env()
    if not config.provider_configured:
        raise SystemExit(f'No provider configured (CALLBOX_PROVIDER={config.provider_kind!r}). '
                         'Set a key in .env first.')
    provider = build_provider(config)
    report = {'suite': 'callbox.live-smoke.v1', 'provider': config.provider_kind,
              'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'budget_usd': budget, 'steps': [], 'total_cost_usd': 0.0}

    def record(name, ok, detail='', cost=0.0):
        report['steps'].append({'name': name, 'ok': bool(ok), 'detail': str(detail),
                                'cost_usd': round(cost, 6)})
        report['total_cost_usd'] = round(report['total_cost_usd'] + cost, 6)
        print(f"{'ok  ' if ok else 'FAIL'} {name:<34} ${cost:.5f}  {detail}")
        if report['total_cost_usd'] > budget:
            raise SystemExit(f"stopping: spent ${report['total_cost_usd']:.4f}, "
                             f"budget was ${budget:.4f}")

    print(f"provider={config.provider_kind} budget=${budget:.2f}\n")

    # 1. Speech generation.
    speech = await provider.speak(PHRASE, wav=True)
    with wave.open(io.BytesIO(speech)) as wav:
        detail = f'{wav.getnframes()} frames @ {wav.getframerate()} Hz'
        rate_ok = wav.getnchannels() == 1 and wav.getsampwidth() == 2
    record('speak returns mono PCM16 WAV', rate_ok, detail, getattr(provider, 'last_cost_usd', 0.0))

    level = wav_rms(speech)
    record('generated speech is audible', level and level > SILENCE_RMS,
           f'RMS {level:.0f} vs threshold {SILENCE_RMS}')

    # 2. Round trip back through transcription.
    heard = await provider.transcribe(speech, 'audio/wav')
    words = {w.strip('.,').lower() for w in heard.split()}
    record('transcribe round trip', {'appointment', 'confirmed', 'tomorrow'} <= words,
           repr(heard), getattr(provider, 'last_cost_usd', 0.0))

    # 3. Intent classification, including the safety-critical case.
    for probe, expected in [('I want to book an appointment for tomorrow', 'book'),
                            ('what time do you open', 'hours'),
                            ('I have chest pain and cannot breathe', {'emergency', 'medical'})]:
        result = await provider.classify(probe, str(datetime.now().date()))
        got = result['intent']
        ok = got in expected if isinstance(expected, set) else got == expected
        record(f'classify {probe[:28]!r}', ok, f'-> {got}', getattr(provider, 'last_cost_usd', 0.0))

    # 4. Silence must be refused locally, spending nothing.
    try:
        heard = await provider.transcribe(silence_wav(), 'audio/wav')
        record('silence refused before paying', False, f'returned {heard!r}')
    except AppError as error:
        record('silence refused before paying', error.code == 'no_speech', error.code)

    # 5. A tone is audible but has no speech: here the sentinel is the only
    #    defence, and models have been observed to confabulate instead.
    try:
        heard = await provider.transcribe(tone_wav(), 'audio/wav')
        record('tone (audible, no speech)', False,
               f'model confabulated: {heard!r}', getattr(provider, 'last_cost_usd', 0.0))
    except AppError as error:
        record('tone (audible, no speech)', error.code == 'no_speech', error.code,
               getattr(provider, 'last_cost_usd', 0.0))

    passed = sum(s['ok'] for s in report['steps'])
    print(f"\n{passed}/{len(report['steps'])} steps, total ${report['total_cost_usd']:.5f}")
    out = ROOT / 'evals' / 'out' / 'live-smoke.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + '\n')
    print(f'wrote {out}')
    return 0 if passed == len(report['steps']) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--budget', type=float, default=0.25,
                        help='hard ceiling in USD (default 0.25)')
    args = parser.parse_args()
    if not 0 < args.budget <= 5:
        parser.error('--budget must be between 0 and 5 USD')
    return asyncio.run(run(args.budget))


if __name__ == '__main__':
    sys.exit(main())
