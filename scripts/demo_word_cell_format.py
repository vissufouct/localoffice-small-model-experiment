"""Real local MLX formatting of table header cells; exact preservation and Undo on synthetic data."""
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
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-cell-format-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx format only the two header cells containing Item and Budget. Set each cell fill to DBEAFE, vertical alignment to center, and top and bottom padding to 160 twips. Batch both cell changes together using format_cell. Preserve all text, other padding, widths, borders, styles and page layout. Stage the working copy and finish.',artifact='report.docx')
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
            cells=doc.findall('.//{'+W+'}tc');assert len(cells)==4
            for cell in cells[:2]:
                props=cell.find('{'+W+'}tcPr')
                shading=props.find('{'+W+'}shd');assert shading is not None and shading.get('{'+W+'}fill')=='DBEAFE'
                align=props.find('{'+W+'}vAlign');assert align is not None and align.get('{'+W+'}val')=='center'
                margins=props.find('{'+W+'}tcMar');assert margins is not None
                assert [(m.tag.split('}')[-1],m.get('{'+W+'}w'),m.get('{'+W+'}type')) for m in margins]==[('top','160','dxa'),('bottom','160','dxa')]
                for e in [shading,align,margins]:props.remove(e)
            assert ET.tostring(doc)==ET.tostring(old_doc)
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'table-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_header_cell_format=True,unrelated_body_and_parts_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-cell-format-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
