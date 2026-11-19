"""Real local model integration demonstration; no mocked inference or tool outcomes."""
import asyncio,json,tempfile,time
from pathlib import Path
from localoffice.providers import LlamaProvider
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner

async def main():
    inventory=json.loads(Path('docs/benchmarks/model-inventory.json').read_text())
    chosen=next(m for m in inventory['models'] if m['architecture']=='nemotron_h' and m['parameters_b']==4)
    folder=Path(tempfile.mkdtemp(prefix='localoffice-demo-'))
    (folder/'source.md').write_text('# Confirmed project facts\n\nProject: Cedar.\nBudget: EUR 120.\nOwner: Ana.\n')
    store=ProjectStore(folder);provider=LlamaProvider();runner=TaskRunner(store,provider)
    report={'folder':str(folder),'model':chosen['name'],'status':'started','tasks':[]}
    started=time.monotonic()
    async def task(prompt,mode):
        key=await runner.start(prompt,mode,'notes.md')
        await asyncio.wait_for(runner.job,180)
        record=dict(store.db.execute('SELECT * FROM tasks WHERE id=?',(key,)).fetchone())
        report['tasks'].append({'task':record,'events':list(runner.events)})
        assert record['status']=='completed',record
        candidate=store.candidate('notes.md');assert candidate,'Expected staged candidate'
        return candidate
    try:
        await provider.start(chosen['path'],4096)
        first=await task('Read source.md. Create notes.md with a Markdown heading "Cedar brief" and exactly three bullets stating the confirmed project name, budget and owner. Do not add other facts. Stage it and finish.','live')
        assert not (folder/'notes.md').exists(),'Original created without Save'
        content=store.read('notes.md')['text']
        assert all(term in content for term in ['Cedar','120','Ana']),content
        store.apply(first['operation_id'],expected_candidate=first['candidate_hash'],action='save')
        original=(folder/'notes.md').read_bytes()
        second=await task('Read notes.md. Change only the heading "Cedar brief" to "Cedar project brief". Keep all bullets unchanged. Stage the result and finish.','background')
        assert (folder/'notes.md').read_bytes()==original,'Original modified before Apply'
        store.apply(second['operation_id'],expected_candidate=second['candidate_hash'],action='apply')
        assert 'Cedar project brief' in (folder/'notes.md').read_text()
        undo=store.undo(second['operation_id'],'demo-undo')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (folder/'notes.md').read_bytes()==original
        report.update(status='passed',originals_preserved_until_apply=True,undo_restored_exact_bytes=True,elapsed_seconds=time.monotonic()-started)
    except Exception as exc:
        report.update(status='failed',error=str(exc),error_type=type(exc).__name__,elapsed_seconds=time.monotonic()-started)
    finally:
        await runner.pause();await provider.close();store.close()
        Path('docs/benchmarks/markdown-demo.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k!='tasks'},indent=2))
    if report['status']!='passed':raise SystemExit(1)
asyncio.run(main())
