#!/usr/bin/env python3
"""Run the CallBox evaluation set and write a scorecard.

    python evals/runner.py             # run all, print a table
    python evals/runner.py --report    # also write evals/out/scorecard.{json,md}
    python evals/runner.py --case booking_happy_path

Makes no paid provider calls: the one provider case uses a mock transport.
"""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.scenarios import REGISTRY, run_all  # noqa: E402

OUT = ROOT / 'evals' / 'out'


def markdown(payload):
    lines = [f"# CallBox evaluation scorecard",
             '',
             f"Generated {payload['generated_at']} · "
             f"**{payload['passed']}/{payload['total']} scenarios passed** · "
             f"{payload['checks_passed']}/{payload['checks_total']} checks",
             '',
             'Cases are the evaluation set named in `docs/ROADMAP.md`. Each scores the reply, the '
             'database ground truth, and whether any reply claimed a capability this release does '
             'not have.',
             '',
             '| Scenario | Roadmap case | Checks | Result |',
             '|---|---|---|---|']
    for case in payload['scenarios']:
        ok = sum(c['ok'] for c in case['checks'])
        lines.append(f"| {case['title']} | {case['roadmap']} | {ok}/{len(case['checks'])} | "
                     f"{'pass' if case['passed'] else '**FAIL**'} |")
    failures = [c for c in payload['scenarios'] if not c['passed']]
    if failures:
        lines += ['', '## Failures', '']
        for case in failures:
            lines.append(f"### {case['title']}")
            if case['error']:
                lines.append(f"- crashed: `{case['error']}`")
            for check in case['checks']:
                if not check['ok']:
                    lines.append(f"- `{check['name']}` {check['detail']}")
            lines.append('')
    lines += ['', '## Ground truth recorded', '']
    for case in payload['scenarios']:
        lines.append(f"- **{case['key']}**: `{json.dumps(case['ground_truth'], default=str)}`")
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--report', action='store_true', help='write evals/out/scorecard.{json,md}')
    parser.add_argument('--case', action='append', help='run only these scenario keys')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    if args.case:
        unknown = set(args.case) - {fn.key for fn in REGISTRY}
        if unknown:
            parser.error('unknown case(s): ' + ', '.join(sorted(unknown)))
        REGISTRY[:] = [fn for fn in REGISTRY if fn.key in args.case]

    with tempfile.TemporaryDirectory(prefix='callbox-evals-') as tmp:
        results = run_all(Path(tmp))

    scenarios = [r.as_dict() for r in results]
    payload = {
        'suite': 'callbox.evals.v1',
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source': 'docs/ROADMAP.md evaluation set',
        'paid_requests': 0,
        'total': len(scenarios),
        'passed': sum(s['passed'] for s in scenarios),
        'checks_total': sum(len(s['checks']) for s in scenarios),
        'checks_passed': sum(c['ok'] for s in scenarios for c in s['checks']),
        'scenarios': scenarios,
    }

    if not args.quiet:
        width = max(len(s['title']) for s in scenarios)
        for case in scenarios:
            ok = sum(c['ok'] for c in case['checks'])
            mark = 'PASS' if case['passed'] else 'FAIL'
            print(f"{mark}  {case['title']:<{width}}  {ok}/{len(case['checks'])} checks")
            if not case['passed']:
                if case['error']:
                    print(f"        crashed: {case['error']}")
                for check in case['checks']:
                    if not check['ok']:
                        print(f"        - {check['name']} {check['detail']}")
        print(f"\n{payload['passed']}/{payload['total']} scenarios, "
              f"{payload['checks_passed']}/{payload['checks_total']} checks, "
              f"{payload['paid_requests']} paid requests")

    if args.report:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'scorecard.json').write_text(json.dumps(payload, indent=2, default=str) + '\n')
        (OUT / 'scorecard.md').write_text(markdown(payload))
        if not args.quiet:
            print(f"\nwrote {OUT / 'scorecard.json'} and {OUT / 'scorecard.md'}")

    return 0 if payload['passed'] == payload['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
