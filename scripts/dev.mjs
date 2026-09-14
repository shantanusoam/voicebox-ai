import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
const local = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const python = process.env.PYTHON || (existsSync(local) ? local : process.platform === 'win32' ? 'python' : 'python3');
const child = spawn(python, ['-m','callbox'], {stdio:'inherit',env:process.env});
child.on('error', err => { console.error('Start failed:', err.message, '\nInstall requirements.txt in a Python virtual environment first.'); process.exitCode=1; });
child.on('exit', code => process.exitCode=code || 0);
for (const signal of ['SIGINT','SIGTERM']) process.on(signal,()=>child.kill(signal));
