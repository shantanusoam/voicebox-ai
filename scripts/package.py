#!/usr/bin/env python3
"""Create a source release, excluding credentials, databases and build caches."""
from pathlib import Path
import hashlib
import zipfile
ROOT=Path(__file__).resolve().parents[1]
output=ROOT.parent/'CallBox-Platform.zip'
excluded={'.runtime','.venv','.git','__pycache__','.pytest_cache','build'}
excluded_files={'.env','server.log','server.pid','overflow-debug.png','initial_browser.py'}
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
    for path in sorted(ROOT.rglob('*')):
        rel=path.relative_to(ROOT)
        if not path.is_file() or any(part in excluded for part in rel.parts):continue
        if path.name in excluded_files or path.suffix in {'.db','.pyc','.zip'}:continue
        if '.db-' in path.name or path.name.startswith('round1-') and path.suffix=='.png':continue
        archive.write(path,'callbox-platform/'+rel.as_posix())
digest=hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix('.sha256').write_text(digest+'  '+output.name+'\n')
print(f'{output} ({output.stat().st_size:,} bytes)\nSHA256 {digest}')
