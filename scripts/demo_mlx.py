"""Reproducible offline MLX read/stage/save gate using existing weights only."""
import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
import psutil
from localoffice.discovery import hardware
from localoffice.providers import MLXProvider
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore

async def run(path,report):
    root=Path(tempfile.mkdtemp(prefix='localoffice-mlx-demo-'))
    (root/'source.md').write_text('Project: Cedar. Budget: EUR 120. Owner: Ana.\n')
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    result={'status':'started','hardware':hardware(),'model_path':path,'context':4096,'project':str(root)}
    started=time.monotonic();swap=psutil.swap_memory().used;peak=0;lowest=psutil.virtual_memory().available
    async def sample():
        nonlocal peak,lowest
        while True:
            lowest=min(lowest,psutil.virtual_memory().available)
            if provider.process:
                try:peak=max(peak,psutil.Process(provider.process.pid).memory_info().rss)
                except psutil.NoSuchProcess:pass
            await asyncio.sleep(.1)
    sampler=asyncio.create_task(sample())
    try:
        await provider.start(path,4096)
        result['model']=provider.model
        task=await runner.start('Read source.md. Create note.md with a short factual Cedar project summary including the budget and owner. Stage it, then finish.','background')
        await asyncio.wait_for(runner.job,240)
        status=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        if status!='completed':raise RuntimeError('Task did not complete: '+status)
        candidate=store.candidate('note.md')
        if not candidate:raise RuntimeError('No note.md candidate')
        text=store.read('note.md')['text']
        if not all(value in text for value in ('Cedar','120','Ana')):raise RuntimeError('Summary lost required source facts')
        if (root/'note.md').exists():raise RuntimeError('Original created without Save')
        source_revision=store.file_hash('source.md')
        if not store.db.execute("SELECT 1 FROM edges WHERE source=? AND target=? AND relation='read-source'",('task:'+task,'revision:'+source_revision)).fetchone():
            raise RuntimeError('Missing exact source-read dependency')
        if not store.db.execute("SELECT 1 FROM edges WHERE source=? AND target='file:note.md' AND relation='targets'",('task:'+task,)).fetchone():
            raise RuntimeError('Missing discovered task target in graph')
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        if (root/'note.md').read_text()!=text:raise RuntimeError('Saved bytes differ from reviewed candidate')
        result.update(status='passed',task_id=task,source_facts_preserved=True,explicit_save_verified=True,exact_source_dependency_verified=True,target_graph_binding_verified=True)
    except Exception as exc:
        result.update(status='blocked' if getattr(exc,'code',None)=='memory_admission' else 'failed',error=str(exc),code=getattr(exc,'code',type(exc).__name__))
    finally:
        await runner.pause();await provider.close();sampler.cancel()
        try:await sampler
        except asyncio.CancelledError:pass
        result.update(elapsed_seconds=time.monotonic()-started,peak_sampled_worker_rss_bytes=peak,minimum_available_bytes=lowest,swap_growth_bytes=psutil.swap_memory().used-swap,events=runner.events)
        store.close();report.parent.mkdir(parents=True,exist_ok=True);report.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('events','model')},indent=2))
    return result['status']=='passed'

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('model');parser.add_argument('--report',type=Path,default=Path('docs/benchmarks/mlx-demo.json'))
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.model,args.report)) else 1)
