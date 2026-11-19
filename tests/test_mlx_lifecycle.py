"""Protocol-process fixtures test lifecycle only, not MLX inference quality."""
import asyncio
import sys
import pytest
from localoffice.providers import MLXProvider
from localoffice.errors import HarnessError

async def worker(code):
    provider=MLXProvider()
    provider.process=await asyncio.create_subprocess_exec(sys.executable,'-u','-c',code,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    provider.model={'name':'protocol fixture'}
    return provider

@pytest.mark.asyncio
async def test_cancelled_count_terminates_worker_and_clears_model():
    provider=await worker('import sys,time; sys.stdin.readline(); time.sleep(30)')
    process=provider.process
    task=asyncio.create_task(provider.count_tokens([{'role':'user','content':'hello'}]))
    await asyncio.sleep(.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert process.returncode is not None
    assert not provider.status()['loaded'] and provider.process is None

@pytest.mark.asyncio
async def test_wrong_count_response_cannot_poison_next_generation():
    provider=await worker('import sys,json,time; sys.stdin.readline(); print(json.dumps({"type":"token","text":"unexpected"}),flush=True); time.sleep(30)')
    process=provider.process
    with pytest.raises(HarnessError) as error:await provider.count_tokens([])
    assert error.value.code=='worker_protocol'
    assert process.returncode is not None
    assert not provider.status()['loaded']

@pytest.mark.asyncio
async def test_valid_count_keeps_worker_available():
    provider=await worker('import sys,json;\nfor line in sys.stdin: print(json.dumps({"type":"count","tokens":17}),flush=True)')
    try:
        assert await provider.count_tokens([])==17
        assert await provider.count_tokens([])==17
        assert provider.status()['loaded']
    finally:await provider.close()

@pytest.mark.asyncio
async def test_generation_transmits_schema_to_owned_worker():
    provider=await worker('import sys,json;\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({"type":"token","text":json.dumps(r["schema"])}),flush=True); print(json.dumps({"type":"done"}),flush=True)')
    try:
        assert provider.status()['constrained_decoding'] is False
        provider.structural_decoding=True
        schema={'type':'object','properties':{'tool':{'const':'finish'}}}
        events=[e async for e in provider.stream([],schema=schema)]
        import json
        assert json.loads(events[0]['text'])==schema
        assert provider.status()['constrained_decoding'] is True
        assert 'bounds and patterns validated by host' in provider.status()['decoding_scope']
    finally:await provider.close()

@pytest.mark.asyncio
async def test_pause_retains_reload_configuration_but_explicit_unload_clears_it(tmp_path,monkeypatch):
    provider=await worker('import sys,time; sys.stdin.readline(); time.sleep(30)')
    provider.model={'name':'fixture','path':str(tmp_path/'weights')}
    provider.log_path=tmp_path/'mlx.log'
    task=asyncio.create_task(provider.count_tokens([]))
    await asyncio.sleep(.03);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not provider.status()['loaded'] and provider.status()['resumable']
    called=[]
    async def restart(*args):
        called.append(args)
        provider.resume_config=None
    monkeypatch.setattr(provider,'start',restart)
    await provider.resume()
    assert called==[(str(tmp_path/'weights'),4096,tmp_path)]
    provider.resume_config=called[0]
    await provider.stop()
    assert not provider.status()['resumable']

@pytest.mark.asyncio
async def test_native_tools_reach_both_count_and_generate():
    provider=await worker('import sys,json;\nfor line in sys.stdin:\n r=json.loads(line); assert r["tools"][0]["function"]["name"]=="finish";\n if r["command"]=="count": print(json.dumps({"type":"count","tokens":23}),flush=True)\n else: print(json.dumps({"type":"token","text":"native"}),flush=True); print(json.dumps({"type":"done"}),flush=True)')
    tools=[{'type':'function','function':{'name':'finish','parameters':{'type':'object'}}}]
    try:
        assert await provider.count_tokens([],tools=tools)==23
        assert [e async for e in provider.stream([],tools=tools)]==[{'type':'token','text':'native'}]
    finally:await provider.close()


def test_native_transport_cannot_enable_legacy_json_grammar():
    with pytest.raises(HarnessError,match='standalone JSON grammar'):
        MLXProvider(native_tool_protocol=True,structural_decoding=True)
    provider=MLXProvider(native_tool_protocol=True)
    assert provider.status()['native_tool_protocol'] is True
    assert provider.status()['constrained_decoding'] is False

@pytest.mark.asyncio
async def test_generation_forwards_explicit_sampling_profile():
    provider=await worker('import sys,json;\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({"type":"token","text":r["sampling_profile"]}),flush=True); print(json.dumps({"type":"done"}),flush=True)')
    try:
        provider.sampling_profile='qwen_recommended'
        events=[e async for e in provider.stream([])]
        assert events==[{'type':'token','text':'qwen_recommended'}]
    finally:await provider.close()
