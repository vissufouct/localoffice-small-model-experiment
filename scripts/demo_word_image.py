"""Offline real-model inline image placement and exact Undo."""
import asyncio,json,sys,tempfile,time,io,zipfile
from xml.etree import ElementTree as ET
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore,digest
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-image-'))
    Image.new('RGB',(300,100),'navy').save(root/'logo.png');source=(root/'logo.png').read_bytes()
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        made=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(made['receipt_id'],expected_candidate=made['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx insert logo.png after the paragraph Evidence-backed introduction. Make the image 3 inches wide, centered, with alt text Navy sample and caption Figure 1 — Navy sample. Preserve all existing content and formatting. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        row=store.db.execute('SELECT metadata FROM image_assets WHERE hash=?',(digest(source),)).fetchone();assert row
        meta=json.loads(row[0]);assert meta['width_px']==300 and meta['height_px']==100
        assert runner.registry.assets.resolve(meta['asset_id'])[1]==source
        candidate=store.candidate('report.docx');assert candidate and (root/'report.docx').read_bytes()==original
        W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        WP='{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            doc=ET.fromstring(new.read('word/document.xml'));body=doc.find(W+'body');paragraphs=body.findall(W+'p')
            assert len(doc.findall('.//'+WP+'docPr'))==1
            assert paragraphs[2].find('.//'+WP+'docPr').get('descr')=='Navy sample'
            assert paragraphs[2].find('.//'+WP+'extent').attrib=={'cx':'2743200','cy':'914400'}
            assert paragraphs[2].find(W+'pPr/'+W+'jc').get(W+'val')=='center'
            assert ''.join(paragraphs[3].itertext())=='Figure 1 — Navy sample'
            body.remove(paragraphs[2]);body.remove(paragraphs[3])
            assert ET.canonicalize(ET.tostring(doc))==ET.canonicalize(old.read('word/document.xml'))
            for name in old.namelist():
                if name not in ('word/document.xml','word/_rels/document.xml.rels','[Content_Types].xml'):assert old.read(name)==new.read(name)
            media=[n for n in new.namelist() if n.startswith('word/media/')]
            assert len(media)==1 and new.read(media[0])==source
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'image-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',source_bytes_verified=True,exact_placement_verified=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-image-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
