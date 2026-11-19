"""Real local MLX existing-list edit; exact preservation and Undo on synthetic data."""
import asyncio
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
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-list-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes()
        before=runner.registry.word.list_objects(path='report.docx',revision=created['candidate_revision'],limit=50)['objects']
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx turn the paragraph Evidence-backed introduction. into a numbered item using the existing decimal list at level zero. Preserve all paragraph text and styles, tables and page layout. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task)
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone();report['selected_skill']=dict(pinned)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        if state!='completed':raise RuntimeError('Task '+state+': '+json.dumps(runner.events[-4:]))
        candidate=store.candidate('report.docx');assert candidate
        assert (root/'report.docx').read_bytes()==original
        paragraph=runner.registry.word.read_objects(path='report.docx',revision=candidate['candidate_hash'],ids=['paragraph:2'],include_format=True)['objects'][0]
        assert paragraph['text']=='Evidence-backed introduction.'
        assert paragraph['direct_format']['properties']['number_id']==2
        assert paragraph['direct_format']['properties']['list_level']==0
        assert paragraph['direct_format']['properties']['style_id']=='Body'
        after=runner.registry.word.list_objects(path='report.docx',revision=candidate['candidate_hash'],limit=50)['objects']
        changed={a['id'] for a,b in zip(before,after) if a['hash']!=b['hash']}
        assert changed=={'paragraph:2'},changed
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0];assert count==1,count
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'list-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_target_paragraph=True,unrelated_object_hashes_unchanged=True,original_unchanged_until_apply=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-list-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
