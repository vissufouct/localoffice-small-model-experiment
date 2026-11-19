"""Real local-model character-style typography with exact preservation/Undo checks."""
import asyncio,json,sys,tempfile,time,io,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_character_styles import character_fixture
from test_word_styles import style_read
from test_word_creation import W
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-character-style-'))
    store,plugin,original,rev=character_fixture(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx change the existing character style Emphasis: make it bold, 14 points and color 17365D. Preserve its inherited italic setting, all other properties, assignments, other styles and document content. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed','Task did not complete'
        candidate=store.candidate('report.docx');assert candidate,'No typography candidate staged'
        assert (root/'report.docx').read_bytes()==original
        props=style_read(plugin,candidate['candidate_hash'],'Emphasis')['direct_format']['properties']
        assert props['bold'] is True and props['size_half_points']=='28' and props['color']=='17365D',props
        assert 'italic' not in props
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/styles.xml'));b=ET.fromstring(new.read('word/styles.xml'))
            changed=next(s for s in b if s.get('{'+W+'}styleId')=='Emphasis')
            run=changed.find('{'+W+'}rPr')
            for tag in ['b','bCs','sz','szCs']:run.remove(run.find('{'+W+'}'+tag))
            run.find('{'+W+'}color').set('{'+W+'}val','333333')
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'style-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',only_requested_typography_changed=True,existing_styles_and_content_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-character-style-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
