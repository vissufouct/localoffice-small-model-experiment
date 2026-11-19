"""Real local-model paragraph-style creation with exact preservation/Undo checks."""
import asyncio,json,sys,tempfile,time,io,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_tables import saved_table
from test_word_styles import style_read
from test_word_creation import W
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-style-create-'))
    store,plugin,original,rev=saved_table(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx create a new paragraph style with ID Callout and name Report callout, based on the existing Body style. Override only bold to true and text color to 17365D. Keep all existing styles and document content unchanged. Do not assign the new style to any paragraphs yet. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed','Task did not complete'
        candidate=store.candidate('report.docx');assert candidate,'No style candidate staged'
        assert (root/'report.docx').read_bytes()==original
        props=style_read(plugin,candidate['candidate_hash'],'Callout')['direct_format']['properties']
        assert props=={'style_id':'Callout','type':'paragraph','default':False,'name':'Report callout','based_on':'Body','bold':True,'color':'17365D'},props
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/styles.xml'));b=ET.fromstring(new.read('word/styles.xml'))
            created=next(s for s in b if s.get('{'+W+'}styleId')=='Callout')
            assert created.attrib=={'{'+W+'}type':'paragraph','{'+W+'}styleId':'Callout','{'+W+'}customStyle':'true'}
            assert [e.tag for e in created]==['{'+W+'}name','{'+W+'}basedOn','{'+W+'}rPr']
            run=created.find('{'+W+'}rPr');assert {e.tag for e in run}=={'{'+W+'}b','{'+W+'}bCs','{'+W+'}color'}
            b.remove(created);assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'style-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',only_requested_style_properties=True,existing_styles_and_content_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-style-create-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
