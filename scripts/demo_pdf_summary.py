"""Real llama.cpp PDF tool use and source-hashed, page-cited Markdown creation."""
import asyncio,json,shutil,tempfile,time
from pathlib import Path
from localoffice.providers import LlamaProvider
from localoffice.storage import ProjectStore,digest
from localoffice.runtime import TaskRunner

async def main():
    model=next(m for m in json.loads(Path('docs/benchmarks/model-inventory.json').read_text())['models'] if m['architecture']=='nemotron_h' and m['parameters_b']==4)
    root=Path(tempfile.mkdtemp(prefix='localoffice-pdf-summary-'));shutil.copyfile('tests/fixtures/pdf/text.pdf',root/'source.pdf')
    original=(root/'source.pdf').read_bytes();revision=digest(original)
    store=ProjectStore(root);provider=LlamaProvider();runner=TaskRunner(store,provider)
    report={'status':'started','project':str(root),'model':model['name'],'source_hash':revision};start=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        key=await runner.start('Use read_pdf to read pages 1 and 2 of source.pdf. Create summary.md with a concise factual summary including project name, budget, owner and deadline. Copy each relevant page citation exactly from the tool evidence, including the revision. Do not invent missing facts. Stage the document and finish.','background','summary.md')
        await asyncio.wait_for(runner.job,180)
        if store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]!='completed':raise RuntimeError('Task failed: '+json.dumps(runner.events[-4:]))
        result=store.read('summary.md')['text']
        for expected in ('Cedar','120','Ana','20 November 2026',f'[source.pdf, p. 1, revision {revision[:12]}]',f'[source.pdf, p. 2, revision {revision[:12]}]'):
            if expected not in result:raise RuntimeError('Missing source fact/citation: '+expected)
        if not any(e['type']=='tool' and e.get('tool')=='read_pdf' for e in runner.events):raise RuntimeError('No actual PDF read')
        if (root/'summary.md').exists():raise RuntimeError('Output saved without approval')
        candidate=store.candidate('summary.md');store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        if (root/'source.pdf').read_bytes()!=original:raise RuntimeError('Source PDF mutated')
        report.update(status='passed',text=result,page_citations_verified=True,source_preserved=True,explicit_save_verified=True,task_id=key)
    except Exception as exc:report.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-start
        Path('docs/benchmarks/pdf-summary-demo.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    return report['status']=='passed'
if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
