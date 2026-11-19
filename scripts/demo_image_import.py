"""Offline real-model image import; no document mutation or model downloads."""
import asyncio,json,sys,tempfile,time
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore,digest
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-image-import-'))
    Image.new('RGB',(300,100),'navy').save(root/'logo.png');source=(root/'logo.png').read_bytes()
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        made=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(made['receipt_id'],expected_candidate=made['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('Import logo.png as a reusable image asset for this project, then finish. Do not insert it into report.docx or change any document yet.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        row=store.db.execute('SELECT metadata FROM image_assets WHERE hash=?',(digest(source),)).fetchone();assert row
        meta=json.loads(row[0]);assert meta['width_px']==300 and meta['height_px']==100
        assert runner.registry.assets.resolve(meta['asset_id'])[1]==source
        assert (root/'report.docx').read_bytes()==original and store.candidate('report.docx') is None
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==0
        report.update(status='passed',source_bytes_verified=True,document_unchanged=True,receipt=meta)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/image-import-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
