"""Bounded real-model probe, using existing weights only. No downloads."""
import asyncio,json,time,sys
from pathlib import Path
import psutil
from localoffice.providers import LlamaProvider
from localoffice.discovery import hardware
from localoffice.runtime import Action

async def main():
    inventory=json.loads(Path('docs/benchmarks/model-inventory.json').read_text())
    selector=sys.argv[1] if len(sys.argv)>1 else 'gemma3/4b'
    chosen=next(m for m in inventory['models'] if selector in m['name'])
    provider=LlamaProvider();started=time.monotonic()
    result={'model':chosen,'hardware':hardware(),'context':4096,'output_reserve':160,'status':'started'}
    try:
        await provider.start(chosen['path'],4096)
        result['load_seconds']=time.monotonic()-started
        before=psutil.swap_memory().used;peak=0;raw='';usage=None
        messages=[{'role':'system','content':'Return a JSON object with tool="finish", path="", text="Local inference is working.", expected_revision=null. No other text.'},{'role':'user','content':'Perform the requested schema check.'}]
        result['input_tokens']=await provider.count_tokens(messages)
        generation=time.monotonic()
        async for event in provider.stream(messages,schema=Action.model_json_schema(),max_tokens=160):
            if event['type']=='token':raw+=event['text']
            if event['type']=='usage':usage=event['usage']
            peak=max(peak,psutil.Process(provider.process.pid).memory_info().rss)
        result.update(generation_seconds=time.monotonic()-generation,output=raw,usage=usage,peak_sampled_rss_bytes=peak,swap_growth_bytes=psutil.swap_memory().used-before)
        parsed=Action.model_validate_json(raw)
        assert parsed.tool=='finish' and parsed.text=='Local inference is working.'
        result['status']='passed'
    except Exception as exc:
        result.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        await provider.close()
        Path('docs/benchmarks/llamacpp-probe-'+chosen['architecture']+'.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k not in ['model','hardware']},indent=2))
    if result['status']!='passed':sys.exit(1)
asyncio.run(main())
