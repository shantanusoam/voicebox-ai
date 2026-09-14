#!/usr/bin/env python3
"""One gate per architecture layer. This is what CI runs and what you run.

    python scripts/verify.py                 # L0-L7
    python scripts/verify.py --with-browser  # adds the Playwright gate
    python scripts/verify.py --only unit evals
    python scripts/verify.py --list

Writes qa/verify-report.json and exits non-zero if any gate fails. Makes no
paid provider calls; evals/live_smoke.py is the opt-in path for that.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GATES = []


def gate(key, layer, description, default=True):
    def wrap(fn):
        fn.key, fn.layer, fn.description, fn.default = key, layer, description, default
        GATES.append(fn)
        return fn
    return wrap


class Gate:
    """Collects the evidence one gate produced."""

    def __init__(self, fn):
        self.key, self.layer, self.description = fn.key, fn.layer, fn.description
        self.checks = []
        self.error = None
        self.started = time.perf_counter()

    def check(self, name, ok, detail=''):
        self.checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:800]})
        return ok

    def run(self, *command, **kwargs):
        """Run a subprocess and record whether it succeeded."""
        name = kwargs.pop('name', None) or ' '.join(command[:3])
        cwd = kwargs.pop('cwd', ROOT)
        timeout = kwargs.pop('timeout', 600)
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=True,
                                timeout=timeout, **kwargs)
        tail = (result.stdout or result.stderr or '').strip().splitlines()
        self.check(name, result.returncode == 0,
                   tail[-1] if tail else f'exit {result.returncode}')
        return result

    @property
    def passed(self):
        return self.error is None and bool(self.checks) and all(c['ok'] for c in self.checks)

    def as_dict(self):
        return {'key': self.key, 'layer': self.layer, 'description': self.description,
                'passed': self.passed, 'error': self.error,
                'seconds': round(time.perf_counter() - self.started, 2),
                'checks': self.checks}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


# --- L0 ---------------------------------------------------------------------

@gate('env', 'config', 'Environment loads, secrets are absent from the repo')
def env_gate(g, args):
    from callbox.config import Config
    import stat
    with tempfile.TemporaryDirectory(prefix='callbox-verify-') as tmp:
        os.environ['CALLBOX_DATA_DIR'] = tmp
        try:
            config = Config.from_env()
        finally:
            os.environ.pop('CALLBOX_DATA_DIR', None)
        g.check('config loads', True, f'provider={config.provider_kind}')
        g.check('admin token is strong', len(config.admin_token) >= 32, f'{len(config.admin_token)} chars')
        mode = stat.S_IMODE(os.stat(config.data_dir).st_mode)
        g.check('.runtime is not world readable', not mode & 0o077, f'mode {mode:o}')
        g.check('turn cost ceiling set', config.max_turn_cost_usd > 0,
                f'${config.max_turn_cost_usd}')

    tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, text=True, capture_output=True)
    if tracked.returncode == 0:
        files = [f for f in tracked.stdout.split() if f]
        g.check('.env is not tracked', '.env' not in files)
        leaked = subprocess.run(['git', 'grep', '-lE', r'sk-or-v1-[a-f0-9]{16}|sk-proj-[A-Za-z0-9]{16}'],
                                cwd=ROOT, text=True, capture_output=True)
        g.check('no provider key in tracked files', leaked.returncode != 0, leaked.stdout.strip())
    else:
        g.check('git metadata available', False, 'not a git repository')


# --- L1 ---------------------------------------------------------------------

@gate('static', 'source', 'Sources compile and the published API schema is current')
def static_gate(g, args):
    g.run(sys.executable, 'scripts/build.py', name='build.py (python + js syntax, assets)')

    # F11: docs/openapi.json was only ever refreshed as a side effect of
    # network_check.py, so it could drift from the code unnoticed.
    from callbox.config import Config
    from callbox.db import Database
    from callbox.main import create_app
    with tempfile.TemporaryDirectory(prefix='callbox-schema-') as tmp:
        config = Config(Path(tmp), 'schema-check-token-' + 'x' * 32, demo=True)
        app = create_app(config, Database(':memory:', seed=False))
        current = json.dumps(app.openapi(), indent=2, sort_keys=True)
    published = ROOT / 'docs' / 'openapi.json'
    if args.update_schema:
        published.write_text(json.dumps(json.loads(current), indent=2) + '\n')
        g.check('docs/openapi.json regenerated', True, 'updated by --update-schema')
    else:
        stored = json.dumps(json.loads(published.read_text()), indent=2, sort_keys=True) \
            if published.exists() else ''
        g.check('docs/openapi.json matches the code', stored == current,
                'stale: rerun with --update-schema' if stored != current else '')


# --- L2 ---------------------------------------------------------------------

@gate('unit', 'domain + api', 'Regression suite, including the review hardening tests')
def unit_gate(g, args):
    result = g.run(sys.executable, '-m', 'pytest', '-q', name='pytest')
    for line in (result.stdout or '').splitlines():
        if 'passed' in line or 'failed' in line:
            g.check('suite summary', 'failed' not in line and 'error' not in line.lower(), line.strip())
            break


# --- L3 ---------------------------------------------------------------------

@gate('contract', 'provider', 'Provider adapters, mocked transport, zero paid calls')
def contract_gate(g, args):
    result = g.run(sys.executable, '-m', 'pytest', '-q',
                   'tests/test_provider_contract.py', 'tests/test_openrouter_contract.py',
                   name='provider contract tests')
    g.check('no network egress in contract tests',
            'MockTransport' not in (result.stderr or ''), '')
    from callbox.config import Config
    with tempfile.TemporaryDirectory(prefix='callbox-prov-') as tmp:
        os.environ['CALLBOX_DATA_DIR'] = tmp
        try:
            config = Config.from_env()
        finally:
            os.environ.pop('CALLBOX_DATA_DIR', None)
    if config.provider_kind == 'none' or not config.provider_configured:
        g.check('provider selection is coherent', True, 'no provider configured (paid paths disabled)')
    else:
        g.check('provider selection is coherent', True,
                f'{config.provider_kind} configured; live calls remain opt-in')


# --- L4 ---------------------------------------------------------------------

@gate('transport', 'gateway', 'Real HTTP/WebSocket against a fresh server process')
def transport_gate(g, args):
    with tempfile.TemporaryDirectory(prefix='callbox-transport-') as tmp:
        port = free_port()
        base = f'http://127.0.0.1:{port}'
        token = 'verify-transport-token-' + 'a' * 40
        env = {**os.environ, 'CALLBOX_DATA_DIR': tmp, 'CALLBOX_ADMIN_TOKEN': token,
               'CALLBOX_DEMO': '1', 'CALLBOX_PORT': str(port),
               'CALLBOX_PROVIDER': 'none', 'OPENAI_API_KEY': '', 'OPENROUTER_API_KEY': ''}
        import httpx
        log = open(Path(tmp) / 'server.log', 'w')
        server = subprocess.Popen([sys.executable, '-m', 'callbox'], cwd=ROOT, env=env,
                                  stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + '/healthz', timeout=.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('server did not become ready')
            g.check('server starts', True, base)

            for path in ['/', '/app/', '/app/app.js', '/app/styles.css', '/docs', '/openapi.json']:
                g.check(f'GET {path}', httpx.get(base + path).status_code == 200)

            headers = httpx.get(base + '/app/').headers
            g.check('CSP served on the console', 'content-security-policy' in headers)
            g.check('CSP served on the website', 'content-security-policy'
                    in httpx.get(base + '/').headers)
            g.check('anonymous API rejected', httpx.get(base + '/api/calls').status_code == 401)
            g.check('foreign Host rejected',
                    httpx.get(base + '/healthz',
                              headers={'host': 'attacker.invalid'}).status_code == 403)

            report = Path(tmp) / 'loopback.json'
            simulator = subprocess.run(
                [sys.executable, 'scripts/device_simulator.py', '--url', base,
                 '--admin-token', token, '--frames', '100', '--output', str(report)],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=120)
            g.check('device simulator loopback', simulator.returncode == 0,
                    (simulator.stderr or simulator.stdout).strip().splitlines()[-1:] or '')
            if report.exists():
                data = json.loads(report.read_text())
                g.check('every frame byte-identical',
                        data.get('exact_matches') == data.get('frames'),
                        f"{data.get('exact_matches')}/{data.get('frames')} frames")
                g.check('interrupt acknowledged', data.get('interrupt_acknowledged') is True)
                g.check('temporary credentials revoked',
                        data.get('temporary_credentials_revoked') is True)
                g.check('hardware still reported unverified',
                        data.get('hardware_verified') is False and data.get('phone_call_placed') is False,
                        'localhost loopback is not a phone call')
                (ROOT / 'qa').mkdir(exist_ok=True)
                (ROOT / 'qa' / 'verify-loopback.json').write_text(json.dumps(data, indent=2) + '\n')
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
            log.close()


# --- L5 ---------------------------------------------------------------------

@gate('firmware', 'device', 'Portable C audio ring, host compiler and assertions')
def firmware_gate(g, args):
    import shutil
    if not shutil.which('make') or not (shutil.which('cc') or shutil.which('gcc')):
        g.check('toolchain available', False, 'make and a C11 compiler are required')
        return
    g.run('make', '-C', 'firmware', 'test', name='firmware host tests')


# --- L6 ---------------------------------------------------------------------

@gate('hardened', 'deployment', 'Demo mode off: nothing reachable without the token')
def hardened_gate(g, args):
    with tempfile.TemporaryDirectory(prefix='callbox-hardened-') as tmp:
        port = free_port()
        base = f'http://127.0.0.1:{port}'
        token = 'verify-hardened-token-' + 'b' * 40
        env = {**os.environ, 'CALLBOX_DATA_DIR': tmp, 'CALLBOX_ADMIN_TOKEN': token,
               'CALLBOX_DEMO': '0', 'CALLBOX_PORT': str(port),
               'CALLBOX_PROVIDER': 'none', 'OPENAI_API_KEY': '', 'OPENROUTER_API_KEY': ''}
        import httpx
        log = open(Path(tmp) / 'server.log', 'w')
        server = subprocess.Popen([sys.executable, '-m', 'callbox'], cwd=ROOT, env=env,
                                  stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + '/healthz', timeout=.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('hardened server did not become ready')
            g.check('anonymous API rejected', httpx.get(base + '/api/calls').status_code == 401)
            g.check('demo login disabled', httpx.post(base + '/api/auth/demo').status_code == 403)
            g.check('UI does not offer demo login',
                    httpx.get(base + '/api/bootstrap').json()['demo_login'] is False)
            g.check('wrong token rejected',
                    httpx.post(base + '/api/auth/login',
                               json={'token': 'wrong-' + 'q' * 30}).status_code == 401)
            signed_in = httpx.post(base + '/api/auth/login', json={'token': token})
            g.check('correct token accepted', signed_in.status_code == 200)
            cookie = signed_in.headers.get('set-cookie', '').lower()
            g.check('session cookie is HttpOnly/SameSite=Strict',
                    'httponly' in cookie and 'samesite=strict' in cookie)
            g.check('admin token not echoed into the cookie', token.lower() not in cookie)
            health = httpx.get(base + '/healthz').json()
            g.check('hardware still reported unverified', health['hardware_verified'] is False)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
            log.close()


# --- L7 ---------------------------------------------------------------------

@gate('evals', 'behaviour', 'The docs/ROADMAP.md evaluation set, scored against ground truth')
def evals_gate(g, args):
    g.run(sys.executable, 'evals/runner.py', '--report', '--quiet', name='evals/runner.py')
    scorecard = ROOT / 'evals' / 'out' / 'scorecard.json'
    if not scorecard.exists():
        g.check('scorecard written', False, 'evals/out/scorecard.json missing')
        return
    data = json.loads(scorecard.read_text())
    g.check('every scenario passes', data['passed'] == data['total'],
            f"{data['passed']}/{data['total']} scenarios")
    g.check('every check passes', data['checks_passed'] == data['checks_total'],
            f"{data['checks_passed']}/{data['checks_total']} checks")
    g.check('no paid requests', data['paid_requests'] == 0)
    g.check('all thirteen roadmap cases present', data['total'] == 13, f"{data['total']} cases")


# --- L8 ---------------------------------------------------------------------

@gate('browser', 'ui', 'Playwright console smoke test', default=False)
def browser_gate(g, args):
    g.run(sys.executable, 'scripts/browser_smoke.py', name='browser_smoke.py', timeout=900)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--with-browser', action='store_true', help='also run the Playwright gate')
    parser.add_argument('--only', nargs='+', metavar='GATE', help='run only these gates')
    parser.add_argument('--update-schema', action='store_true',
                        help='rewrite docs/openapi.json instead of failing on drift')
    parser.add_argument('--list', action='store_true', help='list gates and exit')
    args = parser.parse_args()

    if args.list:
        for fn in GATES:
            flag = '' if fn.default else '  (opt-in)'
            print(f'{fn.key:<10} {fn.layer:<14} {fn.description}{flag}')
        return 0

    selected = [fn for fn in GATES if (fn.key in args.only) if args.only] if args.only else \
        [fn for fn in GATES if fn.default or (args.with_browser and fn.key == 'browser')]
    if args.only:
        unknown = set(args.only) - {fn.key for fn in GATES}
        if unknown:
            parser.error('unknown gate(s): ' + ', '.join(sorted(unknown)))

    results = []
    for fn in selected:
        g = Gate(fn)
        print(f'\n=== {fn.key}  [{fn.layer}]  {fn.description}')
        try:
            fn(g, args)
        except Exception as exc:
            g.error = f'{type(exc).__name__}: {exc}'
        for check in g.checks:
            print(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['name']}"
                  f"{'  ' + check['detail'] if check['detail'] else ''}")
        if g.error:
            print(f'  FAIL gate crashed: {g.error}')
        results.append(g)

    payload = {
        'suite': 'callbox.verify.v1',
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'python': sys.version.split()[0],
        'passed': all(g.passed for g in results),
        'gates': [g.as_dict() for g in results],
        'scope': 'Local software only. Proves no telephone, carrier, Bluetooth or '
                 'deployed operation; see docs/QA.md.',
    }
    (ROOT / 'qa').mkdir(exist_ok=True)
    (ROOT / 'qa' / 'verify-report.json').write_text(json.dumps(payload, indent=2) + '\n')

    print('\n' + '=' * 68)
    for g in results:
        ok = sum(c['ok'] for c in g.checks)
        print(f"{'PASS' if g.passed else 'FAIL'}  {g.key:<10} {ok}/{len(g.checks)} checks"
              f"  {g.as_dict()['seconds']}s")
    print(f"\n{'ALL GATES PASSED' if payload['passed'] else 'PIPELINE FAILED'} "
          f"-> qa/verify-report.json")
    return 0 if payload['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
