from __future__ import annotations
import asyncio
import json
import os
import secrets
import shutil
import socket
import time
from pathlib import Path
import httpx
import psutil
from .errors import HarnessError
from .discovery import inspect_model
from .worker_guard import guarded_command
from .model_profiles import runtime_profile


async def admit_memory(required:int, *, settle_seconds=5):
    """Allow bounded OS reclamation after worker exit; never relax admission."""
    deadline=time.monotonic()+settle_seconds
    available=psutil.virtual_memory().available
    while required<=11*2**30 and available<required and time.monotonic()<deadline:
        await asyncio.sleep(.25)
        available=psutil.virtual_memory().available
    if available<required or required>11*2**30:
        raise HarnessError('memory_admission',f'Model screening requires {required/2**30:.2f} GiB available; currently {available/2**30:.2f} GiB. Close other workloads or choose smaller weights.')
    return available


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class LlamaProvider:
    id='llamacpp'
    def __init__(self):
        self.process=None;self.url=None;self.token=secrets.token_urlsafe(32)
        self.model=None;self.context_limit=4096;self.log_path=None
        self.client=httpx.AsyncClient(timeout=httpx.Timeout(180,connect=3),trust_env=False)

    async def start(self,path: str,context=4096,log_dir: Path|None=None):
        await self.stop()
        path=str(Path(path).expanduser().resolve(strict=True))
        info=inspect_model(Path(path))
        if info['backend']!='llamacpp':raise HarnessError('incompatible_model','Choose GGUF weights for llama.cpp.')
        if info['parameters_b'] and info['parameters_b']>=20:
            raise HarnessError('model_scope','The initial catalogue is below 20B parameters.')
        if not 2048<=context<=8192:raise HarnessError('context_limit','Initial profiles allow 2048–8192 context tokens.')
        # Conservative screening, not physical 16 GB qualification.
        await admit_memory(info['bytes']+2*2**30)
        binary=shutil.which('llama-server')
        if not binary:raise HarnessError('missing_runtime','Install llama.cpp to provide llama-server.')
        port=free_port();self.url=f'http://127.0.0.1:{port}'
        self.context_limit=context
        log_dir=log_dir or Path.home()/'.localoffice/logs'
        log_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.log_path=log_dir/'llamacpp.log'
        key_file=log_dir/'llamacpp.key'
        key_file.write_text(self.token);key_file.chmod(0o600)
        with self.log_path.open('w') as log:
            self.process=await asyncio.create_subprocess_exec(*guarded_command(binary,'--model',path,'--host','127.0.0.1','--port',str(port),
                '--ctx-size',str(context),'--parallel','1','--api-key-file',str(key_file),'--reasoning','off'),
                start_new_session=True,stdout=log,stderr=log,env={**os.environ,'HF_HUB_OFFLINE':'1'})
        start=time.monotonic()
        try:
            while time.monotonic()-start<120:
                if self.process.returncode is not None:
                    raise HarnessError('worker_failure',f'llama.cpp exited. Inspect {self.log_path}.')
                try:
                    response=await self.client.get(self.url+'/health',headers=self.headers)
                    if response.status_code==200:
                        self.model=info;return self.status()
                except httpx.HTTPError:pass
                await asyncio.sleep(.3)
            raise HarnessError('worker_timeout','llama.cpp did not become healthy within 120 seconds.')
        except BaseException:
            await self.stop();raise
        finally:
            key_file.unlink(missing_ok=True)

    @property
    def headers(self):return {'Authorization':'Bearer '+self.token}

    def status(self):
        alive=self.process is not None and self.process.returncode is None
        return {'backend':self.id,'loaded':bool(self.model and alive),'model':self.model,
                'context_limit':self.context_limit,'pid':self.process.pid if alive else None,'log':str(self.log_path) if self.log_path else None,'constrained_decoding':True}

    async def count_tokens(self,messages):
        if not self.status()['loaded']:raise HarnessError('model_disconnected','Load a model first.')
        response=await self.client.post(self.url+'/apply-template',headers=self.headers,json={'messages':messages,'add_generation_prompt':True})
        response.raise_for_status()
        prompt=response.json()['prompt']
        response=await self.client.post(self.url+'/tokenize',headers=self.headers,json={'content':prompt,'add_special':True})
        response.raise_for_status()
        return len(response.json()['tokens'])

    async def stream(self,messages,*,schema=None,max_tokens=768):
        if not self.status()['loaded']:raise HarnessError('model_disconnected','Load a model first.')
        count=await self.count_tokens(messages)
        if count+max_tokens+128>self.context_limit:
            raise HarnessError('context_capacity','Instructions and requested output exceed the effective context budget.')
        payload={'messages':messages,'stream':True,'max_tokens':max_tokens,'temperature':0.2,'stream_options':{'include_usage':True}}
        if schema:payload['response_format']={'type':'json_schema','json_schema':{'name':'operation','strict':True,'schema':schema}}
        async with self.client.stream('POST',self.url+'/v1/chat/completions',headers=self.headers,json=payload) as response:
            if response.status_code!=200:
                await response.aread();raise HarnessError('provider_error',f'llama.cpp returned HTTP {response.status_code}.')
            async for line in response.aiter_lines():
                if not line.startswith('data: '):continue
                value=line[6:]
                if value=='[DONE]':break
                data=json.loads(value)
                for choice in data.get('choices',[]):
                    text=choice.get('delta',{}).get('content')
                    if text:yield {'type':'token','text':text}
                    if choice.get('finish_reason'):yield {'type':'finish','reason':choice['finish_reason']}
                if data.get('usage'):yield {'type':'usage','usage':data['usage']}

    async def stop(self):
        process=self.process
        self.process=None;self.model=None
        if process and process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),5)
            except asyncio.TimeoutError:
                process.kill();await process.wait()

    async def close(self):
        await self.stop();await self.client.aclose()


class MLXProvider:
    id='mlx'
    def __init__(self,*,structural_decoding=False,native_tool_protocol=False,native_constrained=False,sampling_profile="low_temperature"):
        self.process=None;self.model=None;self.context_limit=4096;self.log_path=None
        if structural_decoding and native_tool_protocol:
            raise HarnessError('native_decoding','Native transport cannot use the legacy standalone JSON grammar.')
        if native_constrained and not native_tool_protocol:
            raise HarnessError('native_decoding','Native constraints require the native protocol.')
        from .sampling import settings
        self.sampling=settings(sampling_profile)
        self.sampling_profile=sampling_profile
        self.native_constrained=native_constrained
        self.native_tool_protocol=native_tool_protocol
        self.structural_decoding=structural_decoding
        self.profile=None
        self.lock=asyncio.Lock();self.resume_config=None

    async def start(self,path:str,context=4096,log_dir:Path|None=None):
        import sys
        await self.stop()
        p=Path(path).expanduser().resolve(strict=True)
        info=inspect_model(p)
        if info['backend']!='mlx':raise HarnessError('incompatible_model','Choose verified local MLX-format weights.')
        if info['parameters_b'] and info['parameters_b']>=20:raise HarnessError('model_scope','Initial profiles require below-20B weights.')
        if not 2048<=context<=8192:raise HarnessError('context_limit','Initial context range: 2048–8192.')
        await admit_memory(info['bytes']+2*2**30)
        log_dir=log_dir or Path.home()/'.localoffice/logs';log_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.log_path=log_dir/'mlx.log';self.context_limit=context
        with self.log_path.open('w') as log:
            self.process=await asyncio.create_subprocess_exec(*guarded_command(sys.executable,'-m','localoffice.mlx_worker',str(p)),
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log,
                env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','LOCALOFFICE_MLX_STRUCTURAL_DECODING':'1' if self.structural_decoding else '0','LOCALOFFICE_MLX_NATIVE_DECODING':'1' if self.native_constrained else '0'},limit=1_100_000)
        try:
            event=await self.receive(timeout=120)
            if event['type']!='ready':raise HarnessError('worker_failure',event.get('message','MLX startup failed.'))
            self.model=info
            self.profile=runtime_profile(info,context=context,structural_decoding=self.structural_decoding,native_tool_protocol=self.native_tool_protocol,native_constrained=self.native_constrained,sampling=self.sampling)
            if self.native_tool_protocol and not self.profile['development_candidate_match']:
                raise HarnessError('native_profile','Experimental native transport requires the pinned Qwen model, template and 4096-token context.')
            return self.status()
        except BaseException:
            await self.stop();raise

    def status(self):
        alive=self.process is not None and self.process.returncode is None
        return {'backend':self.id,'loaded':bool(self.model and alive),'model':self.model,
                'context_limit':self.context_limit,'pid':self.process.pid if alive else None,'log':str(self.log_path) if self.log_path else None,
                'sampling_profile':self.sampling_profile,'sampling':dict(self.sampling),'native_tool_protocol':self.native_tool_protocol,'profile':self.profile if self.model and alive else None,
                'resumable':bool(self.resume_config),'constrained_decoding':self.structural_decoding or self.native_constrained,'decoding_scope':('Experimental native wrapper/name/argument structure; bounds and semantics validated by host' if self.native_constrained else 'Experimental JSON structure/types/enums; bounds and patterns validated by host' if self.structural_decoding else 'disabled; full host validation'),'validation':'host schema and semantic validation with bounded retries'}

    async def receive(self,timeout=180):
        line=await asyncio.wait_for(self.process.stdout.readline(),timeout)
        if not line:raise HarnessError('worker_failure','MLX worker exited; inspect its log.')
        try:event=json.loads(line)
        except ValueError:raise HarnessError('worker_protocol','MLX worker returned invalid protocol output.')
        if event['type']=='error':raise HarnessError('worker_failure',event.get('message','MLX failed.'))
        return event

    async def send(self,request):
        if not self.status()['loaded']:raise HarnessError('model_disconnected','Load an MLX model first.')
        self.process.stdin.write((json.dumps(request)+'\n').encode());await self.process.stdin.drain()

    async def count_tokens(self,messages,*,tools=None):
        async with self.lock:
            try:
                await self.send({'command':'count','messages':messages,**({'tools':tools} if tools is not None else {})})
                result=await self.receive()
                if result['type']!='count':raise HarnessError('worker_protocol','Expected tokenizer count.')
                return result['tokens']
            except asyncio.CancelledError:
                config=(self.model['path'],self.context_limit,self.log_path.parent) if self.model and 'path' in self.model else None
                await self.stop();self.resume_config=config
                raise
            except BaseException:
                # A cancelled/failed count may leave a response on stdout. Never
                # let a later generation consume that stale protocol response.
                await self.stop()
                raise

    async def stream(self,messages,*,schema=None,max_tokens=768,tools=None):
        async with self.lock:
            # Structural grammar only; the full host contract still validates every call.
            try:
                await self.send({'command':'generate','sampling_profile':self.sampling_profile,'messages':messages,'schema':schema,'max_tokens':max_tokens,'context_limit':self.context_limit,**({'tools':tools} if tools is not None else {})})
                while True:
                    event=await self.receive()
                    if event['type']=='done':break
                    yield event
            except asyncio.CancelledError:
                config=(self.model['path'],self.context_limit,self.log_path.parent) if self.model and 'path' in self.model else None
                await self.stop();self.resume_config=config
                raise
            except BaseException:
                await self.stop();raise

    async def resume(self):
        async with self.lock:
            if self.status()['loaded']:return
            config=self.resume_config
            if config:
                try:await self.start(*config)
                except BaseException:
                    self.resume_config=config
                    raise

    async def stop(self):
        self.resume_config=None
        process=self.process;self.process=None;self.model=None
        if process and process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),5)
            except asyncio.TimeoutError:process.kill();await process.wait()

    async def close(self):await self.stop()
