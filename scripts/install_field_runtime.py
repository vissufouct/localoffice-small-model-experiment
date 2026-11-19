"""Install the pinned, sub-500 MB Java library dependency; never download models."""
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
manifest=json.loads((ROOT/'docs/benchmarks/java-install.json').read_text())
if manifest['size_bytes']>500_000_000:
    raise SystemExit('This dependency exceeds 500 MB; explicit user approval is required before downloading.')
runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
name=manifest['filename']
if Path(name).name!=name:raise SystemExit('Invalid archive name')
archive=runtime/name
if not archive.exists():
    partial=archive.with_suffix('.partial')
    try:
        with urllib.request.urlopen(manifest['download_url'],timeout=60) as response,partial.open('wb') as output:
            total=0
            while chunk:=response.read(min(1024*1024,manifest['size_bytes']-total+1)):
                total+=len(chunk)
                if total>manifest['size_bytes']:raise RuntimeError('Download exceeds the pinned size')
                output.write(chunk)
        if total!=manifest['size_bytes']:raise RuntimeError('Incomplete download')
        with partial.open('rb') as source:actual=hashlib.file_digest(source,'sha256').hexdigest()
        if actual!=manifest['sha256']:raise RuntimeError('Checksum mismatch')
        partial.replace(archive)
    finally:partial.unlink(missing_ok=True)
with archive.open('rb') as source:actual=hashlib.file_digest(source,'sha256').hexdigest()
if archive.stat().st_size!=manifest['size_bytes'] or actual!=manifest['sha256']:
    raise SystemExit('Existing archive does not match the pinned checksum and size')
if not list((runtime/'java').glob('*/Contents/Home/bin/java')):
    with tarfile.open(archive) as package:package.extractall(runtime/'java',filter='data')
print('Verified local Java runtime ready. Run: .venv/bin/python scripts/build_field_worker.py')
