"""Real llama.cpp selection-scoped refinement; receipts prove what changed."""
import asyncio,json,tempfile,time
from pathlib import Path
from localoffice.providers import LlamaProvider
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.annotations import annotate

async def main():
    inventory=json.loads(Path('docs/benchmarks/model-inventory.json').read_text())
    chosen=next(m for m in inventory['models'] if m['architecture']=='nemotron_h' and m['parameters_b']==4)
    folder=Path(tempfile.mkdtemp(prefix='localoffice-annotation-'))
    original='# Cedar report\n\nThe project is good.\n\nBudget: EUR 120.\nOwner: Ana.\n'
    (folder/'report.md').write_text(original)
    store=ProjectStore(folder);provider=LlamaProvider();runner=TaskRunner(store,provider)
    result={'folder':str(folder),'status':'started','model':chosen['name']}
    started=time.monotonic()
    try:
        await provider.start(chosen['path'],4096)
        annotation=annotate(store,path='report.md',revision=store.file_hash('report.md'),scope='selection',locator={'quote':'The project is good.'},comment='Replace this sentence with exactly: The project is ready for review.')
        key=await runner.start('Read report.md and apply the attached selection annotation exactly. Preserve all other text. Stage the result and finish.','live','report.md',annotation_ids=[annotation])
        await asyncio.wait_for(runner.job,180)
        task=dict(store.db.execute('SELECT * FROM tasks WHERE id=?',(key,)).fetchone())
        assert task['status']=='completed',task
        expected=original.replace('The project is good.','The project is ready for review.')
        assert store.read('report.md')['text']==expected
        assert (folder/'report.md').read_text()==original
        candidate=store.candidate('report.md')
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        assert (folder/'report.md').read_text()==expected
        result.update(status='passed',scope_preserved=True,original_preserved_until_apply=True,task_id=key)
    except Exception as exc:result.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        result['elapsed_seconds']=time.monotonic()-started
        result['events']=runner.events
        await runner.pause();await provider.close();store.close()
        Path('docs/benchmarks/annotation-demo.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k!='events'},indent=2))
    if result['status']!='passed':raise SystemExit(1)
asyncio.run(main())
