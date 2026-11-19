"""Bounded, read-only discovery. Never downloads or converts model weights."""
from __future__ import annotations
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import urllib.request
from pathlib import Path


def gguf_metadata(path: Path) -> dict:
    """Read metadata only (skip arrays); never map tensor weights into memory."""
    formats = {0:'B',1:'b',2:'H',3:'h',4:'I',5:'i',6:'f',7:'?',10:'Q',11:'q',12:'d'}
    with path.open('rb') as stream:
        def number(fmt):
            size=struct.calcsize('<'+fmt)
            data=stream.read(size)
            if len(data)!=size: raise ValueError('Truncated GGUF')
            return struct.unpack('<'+fmt,data)[0]
        def string(keep=True):
            size=number('Q')
            if size>16_000_000: raise ValueError('Oversized metadata')
            if keep:return stream.read(size).decode('utf-8',errors='replace')
            stream.seek(size,1)
        def value(kind,keep=True,depth=0):
            if depth>3:raise ValueError('Nested array limit')
            if kind in formats:return number(formats[kind])
            if kind==8:return string(keep)
            if kind==9:
                sub,count=number('I'),number('Q')
                if count>5_000_000:raise ValueError('Array limit')
                if sub in formats:
                    stream.seek(struct.calcsize('<'+formats[sub])*count,1)
                else:
                    for _ in range(count):value(sub,False,depth+1)
                return None
            raise ValueError('Unknown GGUF type')
        if stream.read(4)!=b'GGUF':raise ValueError('Not GGUF')
        version=number('I')
        if version not in (2,3):raise ValueError('Unsupported GGUF version')
        number('Q')
        count=number('Q')
        if count>100_000:raise ValueError('Metadata limit')
        result={}
        for _ in range(count):
            key=string();kind=number('I')
            keep=key.startswith('general.') and kind!=9
            v=value(kind,keep)
            if keep:result[key]=v
        return result


def parameters_from_label(label):
    match=re.search(r'(?i)(?:^|[-_/ ])(\d+(?:\.\d+)?)b(?:[-_/ ]|$)',label)
    return float(match.group(1)) if match else None


def scan_roots():
    home=Path.home()
    roots=[home/p for p in ['.cache/huggingface/hub','.cache/mlx','models','Models','.lmstudio/models','Library/Caches/llama.cpp','.unsloth/models']]
    for key in ['HF_HOME','HUGGINGFACE_HUB_CACHE','LLAMA_CACHE','LM_STUDIO_MODELS_DIR']:
        if os.environ.get(key):roots.append(Path(os.environ[key]).expanduser())
    return list(dict.fromkeys(roots))


def inspect_model(path: Path, label=None):
    if label is None and path.parent.name=='snapshots' and path.parent.parent.name.startswith('models--'):
        label=path.parent.parent.name.removeprefix('models--').replace('--','/')
    label=label or path.name
    if path.is_file():
        meta=gguf_metadata(path)
        label=meta.get('general.name',label)
        params=meta.get('general.parameter_count')
        return {'id':str(path),'name':label,'path':str(path),'format':'GGUF',
                'backend':'llamacpp','bytes':path.stat().st_size,
                'quantization':meta.get('general.file_type','unknown'),
                'parameters_b':params/1e9 if params else parameters_from_label(meta.get('general.size_label',label)),
                'parameter_evidence':'GGUF count' if params else 'label; not independently counted',
                'architecture':meta.get('general.architecture','unknown'),'state':'downloaded',
                'compatibility':'requires backend probe','metadata':meta}
    config=path/'config.json'
    data=json.loads(config.read_text())
    shards=list(path.glob('*.safetensors'))
    if not shards:raise ValueError('No local safetensors weights')
    q=data.get('quantization',data.get('quantization_config',{}))
    params=None
    index=path/'model.safetensors.index.json'
    if index.exists():
        params=json.loads(index.read_text()).get('metadata',{}).get('total_parameters')
    kind=data.get('model_type','unknown')
    return {'id':str(path),'name':label,'path':str(path),'format':'safetensors',
            'backend':'mlx' if q else 'unknown','bytes':sum(p.stat().st_size for p in shards),
            'quantization':{'bits':q.get('bits'),'group_size':q.get('group_size')},
            'parameters_b':params/1e9 if params else parameters_from_label(label),
            'parameter_evidence':'index metadata' if params else 'label; not independently counted',
            'architecture':kind,'state':'downloaded',
            'compatibility':'requires backend probe' if q else 'MLX format unverified'}


def discover(extra_paths=(), *, probe=True):
    models=[];errors=[];seen=set();visited=0
    def add(p,label=None):
        try:
            identity=str(p.resolve())
            if identity in seen:return
            seen.add(identity)
            model=inspect_model(p,label)
            n=model['parameters_b']
            model['profile']='lightweight candidate' if n and 3<=n<=4.5 else ('optional candidate' if n and 10<=n<=14 else 'custom/unqualified')
            if n and n>=20:model['compatibility']='outside below-20B product scope'
            models.append(model)
        except (OSError,ValueError,KeyError,struct.error) as exc:
            errors.append({'path':str(p),'error':type(exc).__name__})
    roots=scan_roots()
    for supplied in extra_paths:
        p=Path(supplied).expanduser()
        if p.is_file() or (p/'config.json').is_file():add(p)
        elif p.is_dir():roots.append(p)
        else:errors.append({'path':str(p),'error':'not_found'})
    for root in dict.fromkeys(roots):
        if not root.exists():continue
        for base,dirs,files in os.walk(root,followlinks=False):
            visited+=1
            dirs[:]=[d for d in dirs if d not in ('.git','.locks','node_modules')]
            if visited>12000:
                errors.append({'path':str(root),'error':'scan_limit'});break
            for name in files:
                if name.lower().endswith('.gguf'):add(Path(base)/name)
            if 'config.json' in files and any(f.endswith('.safetensors') for f in files):
                folder=Path(base)
                label=folder.parent.parent.name.removeprefix('models--').replace('--','/') if folder.parent.name=='snapshots' else folder.name
                add(folder,label)
    ollama=Path(os.environ.get('OLLAMA_MODELS',str(Path.home()/'.ollama/models'))).expanduser()
    manifests=ollama/'manifests'
    if manifests.is_dir():
        for manifest in list(manifests.rglob('*'))[:1000]:
            if not manifest.is_file():continue
            try:
                data=json.loads(manifest.read_text())
                for layer in data.get('layers',[]):
                    if layer.get('mediaType')=='application/vnd.ollama.image.model':
                        key=layer['digest']
                        if not re.fullmatch(r'sha256:[a-f0-9]{64}',key):continue
                        p=ollama/'blobs'/key.replace(':','-')
                        add(p,str(manifest.relative_to(manifests)))
            except (OSError,ValueError,KeyError):errors.append({'path':str(manifest),'error':'invalid_manifest'})
    endpoints=[]
    if probe:
        for port,route in [(11434,'/api/ps'),(1234,'/v1/models'),(8080,'/v1/models')]:
            url=f'http://127.0.0.1:{port}'
            try:
                with urllib.request.urlopen(url+route,timeout=1) as response:
                    data=json.loads(response.read(200_000))
                entries=data.get('models',data.get('data',[]))
                endpoints.append({'url':url,'status':'reachable','models':[
                    {'name':m.get('name',m.get('id','unknown')),'size':m.get('size'),'loaded':port==11434}
                    for m in entries[:50]],'ownership':'user-managed; never stop automatically'})
            except Exception:endpoints.append({'url':url,'status':'not reachable'})
    return {'models':models,'roots':[str(p) for p in roots],'endpoints':endpoints,'warnings':errors,
            'policy':'Read-only discovery; compatibility and memory admission require measured probes.'}


def hardware():
    import psutil
    def command(*args):
        try:return subprocess.check_output(args,timeout=3,text=True).strip()
        except Exception:return 'unknown'
    return {'platform':platform.platform(),'machine':platform.machine(),
            'chip':command('sysctl','-n','machdep.cpu.brand_string'),
            'memory_bytes':psutil.virtual_memory().total,'available_bytes':psutil.virtual_memory().available,
            'swap_bytes':psutil.swap_memory().used,
            'runtimes':{name:shutil.which(name) for name in ['llama-server','uv','tesseract','cargo','node']}}


if __name__=='__main__':
    print(json.dumps({'hardware':hardware(),**discover()},indent=2))
