"""Validate source and prepare static assets. The API must run as a Python service."""
import compileall
from pathlib import Path
import json
import shutil
import subprocess
ROOT=Path(__file__).resolve().parents[1]
assert compileall.compile_dir(ROOT/'callbox',quiet=1)
if shutil.which('node'):
    subprocess.run(['node','--check',str(ROOT/'web/app/app.js')],check=True)
output=ROOT/'build'
if output.exists():shutil.rmtree(output)
shutil.copytree(ROOT/'web',output/'web')
manifest={'version':'0.2.0','runtime':'Python 3.11+ ASGI','static_only':False,
          'website':'/','console':'/app/','api':'/api/','gateway':'/ws/device',
          'note':'Static files alone do not run the console backend. Start python -m callbox.'}
(output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Validated Python/JavaScript and built web assets in build/web/. Run the API separately.')
