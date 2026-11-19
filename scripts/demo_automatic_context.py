"""Real MLX automatic token-budget recycling with searchable oversized history."""
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

async def main():
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    root=Path(tempfile.mkdtemp(prefix='localoffice-auto-context-'))
    provider=MLXProvider();store=ProjectStore(root);runner=TaskRunner(store,provider)
    report={'status':'started','hardware':hardware(),'model':model,'context_limit':4096,'project':str(root),'resets':[]}
    began=time.monotonic();swap_before=psutil.swap_memory().used;peak=0;available=psutil.virtual_memory().available
    async def sample():
        nonlocal peak,available
        while True:
            available=min(available,psutil.virtual_memory().available)
            if provider.process:
                try:peak=max(peak,psutil.Process(provider.process.pid).memory_info().rss)
                except psutil.NoSuchProcess:pass
            await asyncio.sleep(.1)
    sampler=asyncio.create_task(sample())
    async def task(prompt,**kwargs):
        key=await runner.start(prompt,**kwargs)
        await asyncio.wait_for(runner.job,150)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]
        if state!='completed':raise RuntimeError('Task failed: '+state+' '+json.dumps(runner.events[-3:]))
        return key
    try:
        await provider.start(model['path'],4096)
        source=store.message('user','Confirmed project decisions: Project codename Cedar. Budget EUR 120. Owner Ana. Preserve these facts in later answers.')
        store.decision('Project codename Cedar. Budget EUR 120. Owner Ana.',source,scope='project')
        skill=runner.skills.create_draft('concise-facts','Report confirmed facts concisely.','Use confirmed project facts. Never invent missing figures. Keep answers brief.',['finish'])
        await task('Create report.md with a heading and the three confirmed project facts. Stage it and finish.')
        candidate=store.candidate('report.md')
        if not candidate:raise RuntimeError('No report candidate')
        if (root/'report.md').exists():raise RuntimeError('Original changed before Save')
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        original=(root/'report.md').read_bytes();original_mtime=(root/'report.md').stat().st_mtime_ns
        baseline_ops=store.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        baseline_events=store.db.execute('SELECT count(*) FROM events').fetchone()[0]
        for index in range(3):
            await runner.pause()
            for entry in range(8):
                store.message('tool',f'Synthetic archived evidence marker cycle{index}item{entry}. '+('Historical fixture detail, no new decisions or instructions. '*150))
            store.close();store=ProjectStore(root);runner=TaskRunner(store,provider)
            key=await task('State the confirmed project codename, budget and owner in one short sentence. Use finish only; do not read, stage or change files.',skill_id=skill['id'])
            messages=[json.loads(row[0]) for row in store.db.execute("SELECT content FROM messages WHERE task_id=? AND role='assistant'",(key,))]
            answer=next(m['text'] for m in reversed(messages) if m['tool']=='finish')
            if runner.context_info['omitted_messages']<1:raise RuntimeError('No automatic budget pruning occurred')
            if not store.db.execute("SELECT 1 FROM checkpoints WHERE task_id=? AND json_extract(body,'$.summary_error')='deterministic budget compaction' AND json_extract(body,'$.fallback')=1",(key,)).fetchone():raise RuntimeError('No automatic compaction checkpoint with deterministic fallback')
            if not store.search(f'cycle{index}item0'):raise RuntimeError('Pruned original history is no longer searchable')
            if not all(x in answer for x in ('Cedar','120','Ana')):raise RuntimeError('Decision loss after reset: '+answer)
            if store.db.execute('SELECT count(*) FROM operations').fetchone()[0]!=baseline_ops:raise RuntimeError('Reset created an unexpected write operation')
            if store.db.execute('SELECT count(*) FROM events').fetchone()[0]!=baseline_events:raise RuntimeError('Reset repeated ledger events')
            if (root/'report.md').read_bytes()!=original or (root/'report.md').stat().st_mtime_ns!=original_mtime:raise RuntimeError('Committed file rewritten')
            report['resets'].append({'number':index+1,'task_id':key,'answer':answer,'context':runner.context_info,'operations_unchanged':True,'file_bytes_and_mtime_unchanged':True})
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        await runner.pause();await provider.close();sampler.cancel()
        try:await sampler
        except asyncio.CancelledError:pass
        report.update(elapsed_seconds=time.monotonic()-began,peak_sampled_worker_rss_bytes=peak,minimum_available_bytes=available,swap_growth_bytes=psutil.swap_memory().used-swap_before)
        store.close()
        Path('docs/benchmarks/automatic-context-demo.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','hardware')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
