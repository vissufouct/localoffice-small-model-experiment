"""Real local 4B saved-TOC refresh, raw-cache PDF validation and exact Undo."""
import asyncio,io,json,sys,tempfile,time,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_contents import toc_design
from test_word_field_caches import render_cached,parts,NS
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-saved-toc-'));store=ProjectStore(root)
    provider=MLXProvider();runner=TaskRunner(store,provider);task=None
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**toc_design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save');original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('Update the existing table of contents in report.docx so the saved Word file contains its entries and correct page references. Preserve all other text, formatting and package content. Stage the working copy and finish.',artifact='report.docx');report['task_id']=task
        await asyncio.wait_for(runner.job,150)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0];assert state=='completed',state
        candidate=store.candidate('report.docx');assert candidate
        data=store.blob(candidate['candidate_hash']);old=parts(original);new=parts(data)
        assert old.keys()==new.keys()
        for name in old:
            if name not in ['word/document.xml','word/styles.xml']:assert old[name]==new[name],name
        before=ET.fromstring(old['word/document.xml']);after=ET.fromstring(new['word/document.xml'])
        body=after.find(NS+'body');toc=body[0];assert toc.tag==NS+'sdt'
        assert 'Original report design' in ''.join(toc.itertext()) and 'requires a field update' not in ''.join(toc.itertext())
        anchors={b.get(NS+'name') for b in after.iter(NS+'bookmarkStart')}
        assert anchors and all(h.get(NS+'anchor') in anchors for h in toc.iter(NS+'hyperlink'))
        for paragraph in after.iter(NS+'p'):
            for node in list(paragraph):
                if node.tag in [NS+'bookmarkStart',NS+'bookmarkEnd']:paragraph.remove(node)
        assert [ET.tostring(n) for n in list(before.find(NS+'body'))[1:]]==[ET.tostring(n) for n in list(body)[1:]]
        oldstyles=ET.fromstring(old['word/styles.xml']);newstyles={s.get(NS+'styleId'):ET.tostring(s) for s in ET.fromstring(new['word/styles.xml'])}
        assert all(newstyles[s.get(NS+'styleId')]==ET.tostring(s) for s in oldstyles)
        assert (root/'report.docx').read_bytes()==original
        pdf=await render_cached(data,root/'raw-cache-review')
        assert len(pdf.pages)==3 and pdf.pages[0].extract_text().strip().endswith('2')
        assert 'requires a field update' not in pdf.pages[0].extract_text()
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',revision=candidate['candidate_hash'],raw_pdf=str(root/'raw-cache-review/cached.pdf'),
            saved_cache_verified_without_update=True,unrelated_content_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        if task:
            report['metrics']=summary(store,task)
            pin=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone();report['selected_skill']=dict(pin) if pin else None
        report['events']=runner.events
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-field-caches-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model','events')},indent=2))
    return report['status']=='passed'
if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
