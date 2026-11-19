"""Real local MLX row height and page-splitting edit; exact preservation and Undo on synthetic data."""
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
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-row-format-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx change only the table row containing Cedar and EUR 120. Set minimum row height to 720 twips with rule at_least and prevent the row from splitting across pages (allow_split=false), using format_row. Preserve its text, cells, widths, borders, header row and all other content. Stage the working copy and finish.',artifact='report.docx')
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
            rows=doc.findall('.//{'+W+'}tr');assert len(rows)==2
            props=rows[1].find('{'+W+'}trPr');assert props is not None
            height=props.find('{'+W+'}trHeight');assert height is not None
            assert height.get('{'+W+'}val')=='720' and height.get('{'+W+'}hRule')=='atLeast'
            split=props.find('{'+W+'}cantSplit');assert split is not None and split.get('{'+W+'}val') in ('on','true','1',None)
            assert len(props)==2
            rows[1].remove(props)
            assert ET.tostring(doc)==ET.tostring(old_doc)
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'table-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_row_layout=True,unrelated_body_and_parts_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-row-format-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
