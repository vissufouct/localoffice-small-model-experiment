"""Real local MLX table-layout edit; exact preservation and Undo on synthetic data."""
import asyncio
import os
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-table-layout-'))
    store=ProjectStore(root);provider=MLXProvider(structural_decoding=os.environ.get("LOCALOFFICE_TEST_STRUCTURAL_DECODING")=="1");runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'structural_decoding':provider.structural_decoding,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        before=runner.registry.word.list_objects(path='report.docx',revision=created['candidate_revision'],limit=50)['objects']
        before_table=runner.registry.word.read_objects(path='report.docx',revision=created['candidate_revision'],ids=['table:1'],include_format=True)['objects'][0]
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx center the table, make its first column 3 inches wide and second column 2 inches wide, and set its top and bottom cell padding to 120 twips. Preserve all text, borders, paragraph styles and page layout. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task)
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone();report['selected_skill']=dict(pinned)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        if state!='completed':raise RuntimeError('Task '+state+': '+json.dumps(runner.events[-4:]))
        candidate=store.candidate('report.docx');assert candidate
        assert (root/'report.docx').read_bytes()==original
        table=runner.registry.word.read_objects(path='report.docx',revision=candidate['candidate_hash'],ids=['table:1'],include_format=True)['objects'][0]
        props=table['direct_format']['properties']
        assert props['alignment']=='center' and props['column_widths_twips']==['4320','2880'],{'requested_column_widths':['4320','2880'],'actual':props}
        assert props['layout']=='fixed' and props['width']=='7200'
        padding={p['edge']:p['width'] for p in props['padding']}
        assert padding['top']==padding['bottom']=='120'
        prior_padding={p['edge']:p['width'] for p in before_table['direct_format']['properties']['padding']}
        assert all(padding.get(edge)==prior_padding.get(edge) for edge in ['left','right']), 'Unrequested side padding changed'
        assert table['text']=='Item\tBudget\nCedar\tEUR 120'
        after=runner.registry.word.list_objects(path='report.docx',revision=candidate['candidate_hash'],limit=50)['objects']
        assert len(after)==len(before)
        for a,b in zip(before,after):
            if a['id'].startswith(('paragraph:','section:')):assert a==b
        import io,zipfile
        from xml.etree import ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            assert ET.canonicalize(ET.tostring(a.find('.//'+ns+'tblBorders')))==ET.canonicalize(ET.tostring(b.find('.//'+ns+'tblBorders')))
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'table-layout-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_table_layout=True,unrelated_object_hashes_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-table-layout-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
