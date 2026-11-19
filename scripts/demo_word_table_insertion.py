"""Real local MLX insertion of a new table; exact preservation and Undo on synthetic data."""
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
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-table-insertion-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx insert a new two-column table immediately after the title Original report design, using insert_table. The table must contain exactly two rows: Category | Amount and Training | EUR 250. Set column widths to 3600 and 2400 twips, use Normal paragraph style and repeat the first row as a header. Preserve every existing paragraph, table, style and page setting. Stage the working copy and finish.',artifact='report.docx')
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
            old_body=ET.fromstring(old.read('word/document.xml')).find('{'+W+'}body')
            body=ET.fromstring(new.read('word/document.xml')).find('{'+W+'}body')
            table=list(body)[1];assert table.tag=='{'+W+'}tbl'
            values=[[''.join(c.itertext()) for c in r.findall('{'+W+'}tc')] for r in table.findall('{'+W+'}tr')]
            assert values==[['Category','Amount'],['Training','EUR 250']],values
            widths=[e.get('{'+W+'}w') for e in table.findall('./{'+W+'}tblGrid/{'+W+'}gridCol')]
            assert widths==['3600','2400'],widths
            assert len(table.findall('.//{'+W+'}tblHeader'))==1
            assert [e.get('{'+W+'}val') for e in table.findall('.//{'+W+'}pStyle')]==['Normal']*4
            assert [ET.tostring(e) for e in old_body]==[ET.tostring(e) for e in body if e is not table]
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'table-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_inserted_table=True,unrelated_body_and_parts_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-table-insertion-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
