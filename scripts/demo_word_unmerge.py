"""Real local MLX header-cell unmerge; exact preservation and Undo on synthetic data."""
import io, zipfile
from xml.etree import ElementTree as ET
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design, W
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-unmerge-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        table=runner.registry.word.read_objects(path='report.docx',revision=created['candidate_revision'],ids=['table:1'])['objects'][0]
        merged=runner.registry.word.stage_batch(path='report.docx',revision=created['candidate_revision'],operations=[{'op':'merge_cells','target':'table:1','expected_hash':table['hash'],'row_start':0,'row_count':1,'column_start':0,'column_count':2}],idempotency_key='fixture-merge')
        store.apply(merged['receipt_id'],expected_candidate=merged['candidate_revision'],action='apply')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx unmerge the header cell containing Item and Budget using unmerge_cells, restoring the original two-column table grid. Keep both paragraphs in the left header cell and leave the newly restored right header cell empty. Preserve all body rows, other paragraphs, table grid, styles and page settings. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task)
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone();report['selected_skill']=dict(pinned)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        if state!='completed':raise RuntimeError('Task '+state+': '+json.dumps(runner.events[-4:]))
        candidate=store.candidate('report.docx');assert candidate
        assert (root/'report.docx').read_bytes()==original
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            assert old.namelist()==new.namelist()
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name),name
            old_doc=ET.fromstring(old.read('word/document.xml'));doc=ET.fromstring(new.read('word/document.xml'))
            old_table=old_doc.find('.//{'+W+'}tbl');table=doc.find('.//{'+W+'}tbl')
            old_row=old_table.find('{'+W+'}tr');row=table.find('{'+W+'}tr');cells=row.findall('{'+W+'}tc');assert len(cells)==2
            old_cell=old_row.find('{'+W+'}tc')
            assert [ET.tostring(p) for p in cells[0].findall('{'+W+'}p')]==[ET.tostring(p) for p in old_cell.findall('{'+W+'}p')]
            assert ''.join(cells[1].itertext())==''
            widths=[e.get('{'+W+'}w') for e in table.findall('{'+W+'}tblGrid/{'+W+'}gridCol')]
            for cell,width in zip(cells,widths):
                props=cell.find('{'+W+'}tcPr');assert len(props)==1
                assert props.find('{'+W+'}tcW').get('{'+W+'}w')==width
            assert ET.tostring(row.find('{'+W+'}trPr'))==ET.tostring(old_row.find('{'+W+'}trPr'))
            position=list(table).index(row);table.remove(row);table.insert(position,ET.fromstring(ET.tostring(old_row)))
            assert ET.tostring(doc)==ET.tostring(old_doc)
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'table-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_header_unmerge=True,unrelated_body_and_parts_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-unmerge-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
