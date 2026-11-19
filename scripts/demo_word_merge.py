"""Real local MLX header-cell merge; exact preservation and Undo on synthetic data."""
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
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-merge-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx merge the two header cells Item and Budget into one cell using merge_cells. Keep Item followed by Budget as separate paragraphs, retain the top-left cell formatting, and preserve all body rows, other paragraphs, table grid, styles and page settings. Stage the working copy and finish.',artifact='report.docx')
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
            old_row=old_table.find('{'+W+'}tr');row=table.find('{'+W+'}tr');cells=row.findall('{'+W+'}tc');assert len(cells)==1
            old_cells=old_row.findall('{'+W+'}tc');cell=cells[0]
            assert [ET.tostring(p) for p in cell.findall('{'+W+'}p')]==[ET.tostring(p) for c in old_cells for p in c.findall('{'+W+'}p')]
            props=cell.find('{'+W+'}tcPr');assert len(props)==2
            assert props.find('{'+W+'}gridSpan').get('{'+W+'}val')=='2'
            width=sum(int(c.find('{'+W+'}tcPr/{'+W+'}tcW').get('{'+W+'}w')) for c in old_cells)
            assert props.find('{'+W+'}tcW').get('{'+W+'}w')==str(width)
            assert ET.tostring(row.find('{'+W+'}trPr'))==ET.tostring(old_row.find('{'+W+'}trPr'))
            position=list(table).index(row);table.remove(row);table.insert(position,ET.fromstring(ET.tostring(old_row)))
            assert ET.tostring(doc)==ET.tostring(old_doc)
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'table-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_header_merge=True,unrelated_body_and_parts_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-merge-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
