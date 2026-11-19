"""Real MLX image format request against an existing synthetic picture."""
import asyncio,json,sys,tempfile,time,io,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_image_format import placed,read
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-image-format-'))
    store,plugin,original,revision=placed(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx resize the existing image to 3 inches wide keeping its frame aspect ratio, crop 10 percent from its left edge, and set alt text to Cropped blue image. Preserve all other crop edges, caption, image bytes and document content. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        candidate=store.candidate('report.docx');assert candidate
        assert (root/'report.docx').read_bytes()==original
        props=read(plugin,candidate['candidate_hash'])['direct_format']['properties']
        assert props=={'width_inches':3,'height_inches':2,'crop_left_percent':10,'crop_top_percent':0,'crop_right_percent':0,'crop_bottom_percent':0,'alt_text':'Cropped blue image'},props
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            for doc in (a,b):
                run=next(r for r in doc.iter(W+'r') if r.find(W+'drawing') is not None);run.remove(run.find(W+'drawing'))
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'format-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',geometry_and_crop_verified=True,media_and_caption_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-image-format-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
