"""Real 4B PAGE/NUMPAGES cache refresh with exact preservation and Undo."""
import asyncio,json,sys,tempfile,time
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_page_bands import band_design
from test_word_field_caches import render_cached,parts,NS
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-page-fields-'));store=ProjectStore(root)
    provider=MLXProvider();runner=TaskRunner(store,provider);task=None
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','project':str(root),'model':model,'context':4096};started=time.monotonic()
    try:
        created=runner.registry.word.create(**band_design(),idempotency_key='fixture')
        store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save');original=(root/'report.docx').read_bytes()
        await provider.start(model['path'],4096)
        task=await runner.start('Refresh the existing PAGE and NUMPAGES fields in report.docx so their saved cached results reflect current pagination. Preserve all other content, formatting and native field instructions. Stage the working copy and finish.',artifact='report.docx');report['task_id']=task
        await asyncio.wait_for(runner.job,150)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0];assert state=='completed',state
        candidate=store.candidate('report.docx');assert candidate
        data=store.blob(candidate['candidate_hash']);old=parts(original);new=parts(data)
        assert old.keys()==new.keys()
        for name in old:
            if name!='word/footer1.xml':assert old[name]==new[name],name
        before=ET.fromstring(old['word/footer1.xml']);after=ET.fromstring(new['word/footer1.xml'])
        fields=list(after.iter(NS+'fldSimple'));prior=list(before.iter(NS+'fldSimple'))
        assert [''.join(f.itertext()) for f in fields]==['2','2']
        assert [f.get(NS+'instr') for f in fields]==[' PAGE ',' NUMPAGES ']
        for previous,field in zip(prior,fields):
            assert field.get(NS+'dirty') in ['false','0']
            field.set(NS+'dirty',previous.get(NS+'dirty'))
            for a,b in zip(previous.iter(NS+'t'),field.iter(NS+'t')):b.text=a.text
        assert ET.tostring(before)==ET.tostring(after)
        assert (root/'report.docx').read_bytes()==original
        pdf=await render_cached(data,root/'raw-cache-review')
        assert len(pdf.pages)==2
        for number,page in enumerate(pdf.pages,1):assert f'Page {number} of 2' in ' '.join(page.extract_text().split())
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report.update(status='passed',revision=candidate['candidate_hash'],raw_pdf=str(root/'raw-cache-review/cached.pdf'),
            cached_values_verified_in_xml=True,native_per_page_render_verified=True,unrelated_content_preserved=True,exact_undo=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        if task:
            report['metrics']=summary(store,task)
            pin=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone();report['selected_skill']=dict(pin) if pin else None
        report['events']=runner.events
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-page-field-caches-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model','events')},indent=2))
    return report['status']=='passed'
if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
