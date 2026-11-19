"""Pinned local-model paragraph removal with exact package/intent/Undo assertions."""
import asyncio,io,json,sys,tempfile,time,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design,W
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-deletion-'))
    store=ProjectStore(root);plugin=WordPlugin(store)
    fixture=plugin.create(**design(),idempotency_key='fixture')
    store.apply(fixture['receipt_id'],expected_candidate=fixture['candidate_revision'],action='save')
    original=(root/'report.docx').read_bytes()
    provider=MLXProvider(native_tool_protocol=True,native_constrained=True);runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','model':model,'project':str(root),'context':4096,'native_constrained':True};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        report['profile']=provider.profile
        task=await runner.start('In report.docx remove the entire paragraph whose exact text is Evidence-backed introduction. Preserve every other paragraph, all formatting, lists, tables and the explicit page break. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone()
        report['selected_skill']=dict(pinned) if pinned else None
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed','Task did not complete'
        candidate=store.candidate('report.docx');assert candidate,'No removal candidate staged'
        assert (root/'report.docx').read_bytes()==original
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            body=a.find('{'+W+'}body');matches=[p for p in body if p.tag=='{'+W+'}p' and ''.join(p.itertext())=='Evidence-backed introduction.']
            assert len(matches)==1;body.remove(matches[0])
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b)),'Candidate differs beyond requested paragraph removal'
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        previews=[e for e in runner.events if e['type']=='candidate' and e.get('render')]
        assert len(previews)==1 and previews[0]['revision']==candidate['candidate_hash']
        report['render']=previews[0]['render'];report['candidate_revision']=candidate['candidate_hash']
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',exact_removal=True,unaffected_content_preserved=True,exact_undo=True,visual_review='pending')
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        path=Path('docs/benchmarks/word-deletion-native-'+str(time.time_ns())+'.json');path.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'report':str(path),**{k:v for k,v in report.items() if k not in ('model','metrics','events','profile','render')}},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
