"""Build a source-only public archive; exclude local runtimes, models and state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {'.git','.venv','node_modules','target','dist','bin','obj','classes','__pycache__','.pytest_cache','.runtime','.localoffice','tmp'}
ALLOWED_ROOTS = {'host','apps','packages','workers','tests','scripts','docs'}
ALLOWED_FILES = {'README.md','LICENSE','THIRD_PARTY_NOTICES.md','AGENTS.md','.gitignore','package.json','package-lock.json','pyproject.toml','uv.lock','tsconfig.json','Setup.command','Start LocalOffice.command'}
FORBIDDEN_SUFFIXES = {'.gguf','.safetensors','.sqlite3','.db','.pyc','.log','.zip','.dmg'}

def sources():
    result=[]
    for path in ROOT.rglob('*'):
        rel=path.relative_to(ROOT)
        if any(p in EXCLUDE for p in rel.parts) or path.is_symlink() or not path.is_file():continue
        if rel.parts[0] not in ALLOWED_ROOTS and str(rel) not in ALLOWED_FILES:continue
        if path.suffix in FORBIDDEN_SUFFIXES:raise ValueError('Unexpected private/binary file: '+str(rel))
        if path.stat().st_size > 10_000_000:raise ValueError('Unexpected large file: '+str(rel))
        data=path.read_bytes()
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:text=''
        patterns=[r'/' + r'Users/[^/\s"<>]+/',r'/var/' + r'folders/[^\s"<>]+',r'\b(?:hf_|sk-)[A-Za-z0-9]{20,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
        if any(re.search(p,text) for p in patterns):raise ValueError('Privacy-pattern match: '+str(rel))
        result.append((rel,data))
    return sorted(result)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    files=sources()
    manifest={'purpose':'Source-only experiment export','files':[{'path':str(r),'sha256':hashlib.sha256(d).hexdigest(),'bytes':len(d)} for r,d in files], 'exclusions':sorted(EXCLUDE),'privacy_scan':'Known home/temp paths, token patterns and private-key markers; not a security guarantee.'}
    with zipfile.ZipFile(a.output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for rel,data in files:
            info=zipfile.ZipInfo('LocalOfficeHarness-Experiment/'+str(rel));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=(0o100755 if rel.suffix=='.command' else 0o100644)<<16
            z.writestr(info,data)
        z.writestr('LocalOfficeHarness-Experiment/PACKAGE_MANIFEST.json',json.dumps(manifest,indent=2)+'\n')
    with zipfile.ZipFile(a.output) as z:assert z.testzip() is None
    print(json.dumps({'archive':str(a.output),'files':len(files)+1,'bytes':a.output.stat().st_size,'sha256':hashlib.sha256(a.output.read_bytes()).hexdigest()}))

if __name__=='__main__':main()
