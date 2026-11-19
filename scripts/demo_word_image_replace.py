"""Real MLX image replacement request against an existing synthetic picture."""
import asyncio,json,sys,tempfile,time,io,zipfile
from pathlib import Path
from PIL import Image
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_image_format import placed,read
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-image-replace-'))
    store,plugin,original,revision=placed(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    Image.new('RGB',(300,200),'gold').save(root/'replacement.png')
    source=(root/'replacement.png').read_bytes()
    original_format=read(plugin,revision)['direct_format']
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx replace the existing picture with replacement.png. Keep its current frame size, position, crop, alt text, caption and all other document content unchanged. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        candidate=store.candidate('report.docx');assert candidate
        assert (root/'report.docx').read_bytes()==original
        assert read(plugin,candidate['candidate_hash'])['direct_format']==original_format
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            assert set(old.namelist())<=set(new.namelist())
            for name in old.namelist():
                if name not in ('word/document.xml','word/_rels/document.xml.rels','[Content_Types].xml'):assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            A='{http://schemas.openxmlformats.org/drawingml/2006/main}'
            R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
            before=list(a.iter(A+'blip'));after=list(b.iter(A+'blip'))
            assert len(before)==len(after)==1
            oldrid=before[0].get(R+'embed');newrid=after[0].get(R+'embed');assert oldrid!=newrid
            rels=ET.fromstring(new.read('word/_rels/document.xml.rels'))
            rel=next(r for r in rels if r.get('Id')==newrid)
            assert new.read(rel.get('Target').lstrip('/'))==source
            after[0].set(R+'embed',oldrid)
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'format-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',replacement_bytes_verified=True,other_drawing_properties_and_content_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-image-replace-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
