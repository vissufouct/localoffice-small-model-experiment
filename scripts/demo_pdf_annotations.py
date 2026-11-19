"""Real MLX source annotations refine Markdown without editing source PDFs."""
import asyncio
import json
import tempfile
import time
from pathlib import Path
from localoffice.annotations import annotate
from localoffice.providers import MLXProvider
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore


async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-pdf-annotations-'))
    source=Path('tests/fixtures/pdf/text.pdf').read_bytes()
    (root/'source.pdf').write_bytes(source)
    original='# Cedar brief\n\nOwner: [unknown].\nDelivery: [unknown].\nBudget: EUR 120.\n'
    (root/'note.md').write_text(original)
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['local_path']
    report={'status':'started','project':str(root),'model':model,'context':4096,'cases':[]}
    began=time.monotonic()
    try:
        await provider.start(model,4096)
        expected=original
        for scope,page,quote,instruction,before,after in [
            ('selection',1,'Owner: Ana.','Use this source selection to replace only Owner: [unknown]. with Owner: Ana. in note.md. Preserve every other byte.','Owner: [unknown].','Owner: Ana.'),
            ('page',2,'','Use page 2 to replace only Delivery: [unknown]. with Delivery: 20 November 2026. in note.md. Preserve every other byte.','Delivery: [unknown].','Delivery: 20 November 2026.')]:
            annotation=annotate(store,path='source.pdf',revision=store.file_hash('source.pdf'),scope=scope,locator={'page':page,'quote':quote},comment=instruction)
            target_annotation=annotate(store,path='note.md',revision=store.read('note.md')['revision'],scope='selection',locator={'quote':before},comment='Replace only this selected placeholder sentence using the attached PDF evidence. Preserve all other bytes, including the final newline.')
            task=await runner.start(f'Read source.pdf page {page} using read_pdf and read note.md. Apply the attached source annotation to note.md, stage the corrected Markdown and finish. The source PDF remains read-only.','background','note.md',annotation_ids=[annotation,target_annotation])
            await asyncio.wait_for(runner.job,180)
            assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed',runner.events[-3:]
            expected=expected.replace(before,after)
            assert store.read('note.md')['text']==expected,store.read('note.md')['text']
            assert (root/'note.md').read_text()==original
            assert (root/'source.pdf').read_bytes()==source
            scope_row=store.db.execute('SELECT * FROM task_scopes WHERE task_id=?',(task,)).fetchone()
            assert scope_row['path']=='note.md'
            assert json.loads(scope_row['ranges'])==[[store.blob(scope_row['base_revision']).decode().index(before),store.blob(scope_row['base_revision']).decode().index(before)+len(before)]]
            assert store.db.execute("SELECT count(*) FROM nodes WHERE kind='source_view' AND json_extract(body,'$.page')=?",(page,)).fetchone()[0]>0
            report['cases'].append({'scope':scope,'page':page,'annotation_id':annotation,'target_annotation_id':target_annotation,'task_id':task,'events':list(runner.events),'exact_target_bytes_verified':True,'source_unchanged':True,'source_not_treated_as_target_patch_boundary':True})
        candidate=store.candidate('note.md')
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        assert (root/'note.md').read_text()==expected
        assert not store.db.execute("SELECT 1 FROM operations WHERE path='source.pdf'").fetchone()
        report.update(status='passed',explicit_apply_verified=True,no_pdf_write_operations=True)
    except Exception as exc:
        report.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        await runner.pause();await provider.close();store.close()
        report['elapsed_seconds']=time.monotonic()-began
        Path('docs/benchmarks/pdf-annotations-demo.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2))
    return report['status']=='passed'


if __name__=='__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
