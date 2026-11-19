"""Real model pause after staging, worker/store restart, annotate and continue."""
import asyncio,json,tempfile,time
from pathlib import Path
from localoffice.providers import LlamaProvider
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.annotations import annotate

async def main():
    inventory=json.loads(Path('docs/benchmarks/model-inventory.json').read_text())
    model=next(m for m in inventory['models'] if m['architecture']=='nemotron_h' and m['parameters_b']==4)
    root=Path(tempfile.mkdtemp(prefix='localoffice-resume-demo-'))
    original='# Cedar\n\nThe project is good.\n\nBudget: EUR 120.\nOwner: Ana.\n'
    (root/'report.md').write_text(original)
    store=ProjectStore(root);provider=LlamaProvider();runner=TaskRunner(store,provider)
    report={'status':'started','project':str(root),'model':model['name']};began=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        task=await runner.start('Read report.md. Replace only "The project is good." with "The project is ready for review." Stage the full updated document and finish.','live','report.md')
        deadline=time.monotonic()+150
        while not store.candidate('report.md'):
            if runner.job.done():raise RuntimeError('Task ended before a working copy existed')
            if time.monotonic()>deadline:raise TimeoutError('No working copy within deadline')
            await asyncio.sleep(.01)
        await runner.pause()
        if store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]!='paused':raise RuntimeError('Missed unfinished-task pause boundary')
        first=store.candidate('report.md');expected_first=original.replace('The project is good.','The project is ready for review.')
        if store.read('report.md')['text']!=expected_first:raise RuntimeError('Initial revision changed unexpected content')
        if (root/'report.md').read_text()!=original:raise RuntimeError('Original changed before Apply')
        await provider.close();store.close()
        provider=LlamaProvider()
        store=ProjectStore(root);runner=TaskRunner(store,provider)
        if runner.active!=task or store.candidate('report.md')!=first:raise RuntimeError('Task or candidate lost after reopening')
        feedback=annotate(store,path='report.md',revision=first['candidate_hash'],scope='selection',locator={'quote':'The project is ready for review.'},comment='Replace this sentence with exactly: The project is approved for drafting.')
        await provider.start(model['path'],4096)
        resumed=await runner.start('Continue the same task using the new annotation, which supersedes the earlier wording. Read the current working copy. Replace the selected sentence exactly as requested and preserve all other text. Stage the change and finish.','background','report.md',task_id=task,annotation_ids=[feedback])
        await asyncio.wait_for(runner.job,150)
        if resumed!=task:raise RuntimeError('Continuation created a different task')
        if store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]!='completed':raise RuntimeError('Resumed task failed: '+json.dumps(runner.events[-3:]))
        expected=original.replace('The project is good.','The project is approved for drafting.')
        if store.read('report.md')['text']!=expected:raise RuntimeError('Annotation was not followed exactly')
        if (root/'report.md').read_text()!=original:raise RuntimeError('Original changed before Apply')
        if store.db.execute('SELECT count(*) FROM operations').fetchone()[0]!=2:raise RuntimeError('Unexpected duplicate staging operation')
        candidate=store.candidate('report.md')
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        if (root/'report.md').read_text()!=expected:raise RuntimeError('Apply mismatch')
        report.update(status='passed',same_task_resumed=True,worker_restarted=True,live_then_background=True,annotation_scope_preserved=True,original_unchanged_until_apply=True,exactly_two_staging_operations=True)
    except Exception as exc:report.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        await runner.pause();await provider.close();store.close()
        report['elapsed_seconds']=time.monotonic()-began
        Path('docs/benchmarks/resume-demo.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    return report['status']=='passed'
if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
