"""Explicitly authorized serial inference on frozen admitted prompts; never dispatch."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PIN = '50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
ORDER = ('production', 'production_with_review_tools', 'completion_review')
REJECTED = 'review_with_production_tools'
SAMPLING = {'temperature': 0.2, 'top_p': 1.0, 'top_k': 0, 'min_p': 0.0}


def sha(data): return hashlib.sha256(data).hexdigest()
def packed(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def runtime_sources():
    return {str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in (ROOT/'host/localoffice').rglob('*')
            if p.is_file() and p.suffix in ('.py', '.md')}


async def run(args):
    # Atomic exclusive reservation before any model startup. Never replace output.
    with args.output.open('x', encoding='utf-8') as output:
        result = {'diagnostic':'terminal-state frozen selection only', 'acceptance_evidence':False,
                  'actions_dispatched':0,'ledger_updates':0,'retries':0,'runs':[],
                  'limits':{'context':4096,'output':768,'safety':128,'input':3200},
                  'status':'preflight','model_started':False,'sampling':SAMPLING,
                  'runner_sha256':sha(Path(__file__).read_bytes())}
        provider = None
        started = time.monotonic()
        preflight_bytes = None
        sources = None
        try:
            os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
            preflight_bytes = args.preflight.read_bytes()
            frozen = json.loads(preflight_bytes)
            sources = runtime_sources()
            result.update(preflight_path=str(args.preflight.resolve()),preflight_sha256=sha(preflight_bytes),
                          runtime_sources=sources,runtime_sha256=sha(packed(sources)),tokenizer_revision=PIN)
            assert sources==frozen['runtime_sources'] and frozen['runtime_stable'] is True, 'Runtime differs from frozen preflight'
            assert frozen['tokenizer_revision']==PIN and frozen['snapshot_kind']=='terminal_recorded_state'
            assert frozen['source_db_before']==frozen['source_db_after']
            assert frozen['preservation']=={'non_system_messages_identical':True,'authoritative_state_unchanged':True}
            variants = frozen['variants']
            assert set(variants)==set(ORDER)|{REJECTED}
            assert variants[REJECTED]['admitted'] is False and variants[REJECTED]['tokens']>3200
            result['skipped'] = {REJECTED:{'tokens':variants[REJECTED]['tokens'],'reason':'frozen admission rejected'}}
            payloads = {name:{'messages':variants[name]['messages'],'tools':variants[name]['tools']} for name in ORDER}
            payload_hashes = {name:sha(packed(payload)) for name,payload in payloads.items()}
            result['payload_sha256'] = payload_hashes
            result['frozen_payloads'] = payloads
            sys.path.insert(0,str(ROOT/'host'))
            from localoffice.providers import MLXProvider
            from localoffice.native_tools import parse_qwen_call, render_prompt, validate_history
            from transformers import AutoTokenizer
            approval = json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
            model_path = Path(approval['model']['path']).resolve(strict=True)
            assert approval['revision']==PIN and model_path.name==PIN
            assert model_path==Path(approval['local_path']).resolve(strict=True)
            result['model_path'] = str(model_path)
            tokenizer = AutoTokenizer.from_pretrained(model_path,local_files_only=True,trust_remote_code=False)
            result['tokenizer_files'] = {p.name:sha(p.read_bytes()) for p in model_path.iterdir()
                if p.name in ('tokenizer.json','tokenizer_config.json','special_tokens_map.json','chat_template.jinja')}
            counts = {}
            for name,payload in payloads.items():
                assert variants[name]['admitted'] is True
                validate_history(payload['messages'])
                n = len(tokenizer.encode(render_prompt(tokenizer,**payload)))
                assert n==variants[name]['tokens'] and n+768+128<=4096, 'Frozen token count/admission mismatch'
                counts[name] = n
            result['reverified_token_counts'] = counts

            def fresh():
                assert args.preflight.read_bytes()==preflight_bytes, 'Preflight changed'
                assert runtime_sources()==sources, 'Runtime changed'
                assert {n:sha(packed(p)) for n,p in payloads.items()}==payload_hashes, 'Frozen payload changed'

            fresh()
            provider = MLXProvider(structural_decoding=False,native_tool_protocol=True,
                                   native_constrained=True,sampling_profile='low_temperature')
            assert provider.sampling==SAMPLING
            with tempfile.TemporaryDirectory(prefix='completion-selection-worker-') as logs:
                try:
                    await asyncio.wait_for(provider.start(str(model_path),4096,log_dir=Path(logs)),150)
                    result['model_started'] = True
                    result['provider_status'] = provider.status()
                    # Reverify every admitted payload with the actual worker before generating any.
                    result['worker_token_counts'] = {}
                    for name,payload in payloads.items():
                        fresh()
                        n = await asyncio.wait_for(provider.count_tokens(**payload),60)
                        result['worker_token_counts'][name] = n
                        assert n==counts[name] and n+896<=4096, 'Worker count mismatch'
                    for name in ORDER:
                        fresh()
                        item = {'variant':name,'payload_sha256':payload_hashes[name],
                                'input_tokens':counts[name],'raw':'','events':[],'status':'generating'}
                        result['runs'].append(item)
                        then = time.monotonic()
                        async def generate():
                            async for event in provider.stream(**payloads[name],max_tokens=768):
                                item['events'].append(event)
                                if event['type']=='token':item['raw']+=event['text']
                        try:
                            await asyncio.wait_for(generate(),150)
                            try:
                                item['parsed_action'] = parse_qwen_call(item['raw'],tools=payloads[name]['tools'])
                                item['status'] = 'parsed_not_dispatched'
                            except Exception as exc:
                                item.update(status='parse_rejected',parse_error=repr(exc))
                        except BaseException:
                            item['status'] = 'generation_failed_no_retry'
                            raise
                        finally:
                            item['elapsed_seconds'] = time.monotonic()-then
                            item['raw_sha256'] = sha(item['raw'].encode())
                        fresh()
                    result['status'] = 'diagnostic_completed_no_actions'
                finally:
                    await provider.stop()
                    log_path = Path(logs)/'mlx.log'
                    if log_path.exists():
                        data = log_path.read_bytes()
                        result['worker_log'] = {'sha256':sha(data),'bytes':len(data),
                            'tail':data[-65536:].decode(errors='replace'),'truncated':len(data)>65536}
        except BaseException as exc:
            result.update(status='failed_no_retry',error=repr(exc))
        finally:
            if provider is not None: await provider.stop()
            result['elapsed_seconds'] = time.monotonic()-started
            try:
                result['preflight_unchanged'] = preflight_bytes is not None and args.preflight.read_bytes()==preflight_bytes
                result['runtime_unchanged'] = sources is not None and runtime_sources()==sources
            except Exception as exc:
                result.update(preflight_unchanged=False,runtime_unchanged=False,final_verification_error=repr(exc))
            if not (result['preflight_unchanged'] and result['runtime_unchanged']):
                result['status'] = 'invalidated_source_change_or_unverified'
            json.dump(result,output,ensure_ascii=False,indent=2); output.write('\n'); output.flush()
            os.fsync(output.fileno())
        print(json.dumps({'status':result['status'],'runs':[{k:r.get(k) for k in ('variant','status','parsed_action')} for r in result['runs']]}))
        return result['status']=='diagnostic_completed_no_actions'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)
