"""Real local-model span formatting with character-level preservation and exact Undo."""
import asyncio,json,sys,tempfile,time,io,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_tables import saved_table
from test_word_text_format import characters,N
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-text-format-'))
    store,plugin,original,rev=saved_table(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx make only the text Evidence-backed bold in the introduction paragraph. Preserve the following space, remaining text, all other formatting and all other document content. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed','Task did not complete'
        candidate=store.candidate('report.docx');assert candidate,'No text-format candidate staged'
        assert (root/'report.docx').read_bytes()==original
        data=store.blob(candidate['candidate_hash']);before=characters(original)[1];after=characters(data)[1]
        assert len(before)==len(after)
        for i,(a,b) in enumerate(zip(before,after)):
            assert a[0]==b[0] and a[2]==b[2]
            expected={**a[1],N+'b':{N+'val':'true'},N+'bCs':{N+'val':'true'}} if i<15 else a[1]
            assert b[1]==expected,{'character':i,'expected':expected,'actual':b[1]}
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(data)) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            for doc in (a,b):
                body=doc.find(N+'body');body.remove(body.findall(N+'p')[1])
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'text-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',only_requested_characters_formatted=True,other_content_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-text-format-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
