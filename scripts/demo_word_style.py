"""Real local-model style-definition edit with exact preservation and Undo."""
import asyncio,json,sys,tempfile,time,io,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design,W
from xml.etree import ElementTree as ET
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-style-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096,'structural_decoding':False};started=time.monotonic()
    try:
        made=runner.registry.word.create(**design(),idempotency_key='fixture')
        store.apply(made['receipt_id'],expected_candidate=made['candidate_revision'],action='save');original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('In report.docx change the existing Body paragraph style definition to Arial, 14 point, bold and dark blue 17365D. Preserve its spacing and all other settings, other styles, paragraph memberships and content. Stage the working copy and finish.',artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,task);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        candidate=store.candidate('report.docx');assert candidate
        assert (root/'report.docx').read_bytes()==original
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as new:
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/styles.xml'));b=ET.fromstring(new.read('word/styles.xml'))
            for sa,sb in zip(a,b):
                if sa.get('{'+W+'}styleId')!='Body':assert ET.canonicalize(ET.tostring(sa))==ET.canonicalize(ET.tostring(sb))
                else:
                    assert ET.canonicalize(ET.tostring(sa.find('{'+W+'}pPr')))==ET.canonicalize(ET.tostring(sb.find('{'+W+'}pPr'))), 'Unrequested paragraph-style layout changed'
                    r=sb.find('{'+W+'}rPr');assert r.find('{'+W+'}sz').get('{'+W+'}val')=='28', 'Expected 14 points / 28 half-points'
                    assert r.find('{'+W+'}rFonts').get('{'+W+'}ascii')=='Arial'
                    assert r.find('{'+W+'}b') is not None
                    assert r.find('{'+W+'}color').get('{'+W+'}val')=='17365D'
                    # Preserve the definition beyond the explicitly requested
                    # typography, including inherited/absent italic semantics.
                    import copy
                    aa,bb=copy.deepcopy(sa),copy.deepcopy(sb)
                    requested={'rFonts','sz','szCs','b','bCs','color'}
                    for style in (aa,bb):
                        rp=style.find('{'+W+'}rPr')
                        for child in list(rp):
                            if child.tag in {'{'+W+'}'+name for name in requested}:rp.remove(child)
                    assert ET.canonicalize(ET.tostring(aa))==ET.canonicalize(ET.tostring(bb)), 'Unrequested style properties changed'
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',style_verified=True,unrelated_parts_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-style-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('model','metrics','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
