"""Real 4B native TOC creation plus updated PDF; saved DOCX cache is explicitly unqualified."""
import asyncio,io,json,tempfile,time,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from pypdf import PdfReader
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-contents-'))
    store=ProjectStore(root);provider=MLXProvider();runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','model':model,'context':4096,'project':str(root),
            'scope':'Native TOC creation and populated PDF preview; saved DOCX cache update remains unqualified.'}
    started=time.monotonic();task=None
    try:
        await provider.start(model['path'],4096)
        prompt=('Create a draft report.docx with a native table of contents on page 1, then a page break, '
                'then exactly these two body paragraphs on page 2: heading "Findings" at heading level 1, '
                'and normal paragraph "The pilot is ready for review.". Use those texts literally. '
                'Use Arial 11 point body and 16 point bold heading, A4 portrait. Stage the working copy '
                'with a populated PDF preview and finish. The saved DOCX TOC cache can remain pending; disclose that limitation.')
        task=await runner.start(prompt,artifact='report.docx');report['task_id']=task
        await asyncio.wait_for(runner.job,150)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        assert state=='completed',state
        candidate=store.candidate('report.docx');assert candidate
        data=store.blob(candidate['candidate_hash']);W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            doc=ET.fromstring(package.read('word/document.xml'));styles=ET.fromstring(package.read('word/styles.xml'))
            paragraphs=doc.findall(W+'body/'+W+'p')
            assert len(paragraphs)==3
            assert paragraphs[0].find('.//'+W+'instrText').text.strip().startswith('TOC')
            assert [''.join(p.itertext()) for p in paragraphs[1:]]==['Findings','The pilot is ready for review.']
            assert paragraphs[1].find('.//'+W+'pageBreakBefore') is not None
            stylemap={s.get(W+'styleId'):s for s in styles.findall(W+'style')}
            for paragraph,half_points in zip(paragraphs[1:],['32','22']):
                style=stylemap[paragraph.find(W+'pPr/'+W+'pStyle').get(W+'val')]
                assert style.find('.//'+W+'sz').get(W+'val')==half_points
                assert style.find('.//'+W+'rFonts').get(W+'ascii')=='Arial'
            heading=stylemap[paragraphs[1].find(W+'pPr/'+W+'pStyle').get(W+'val')]
            assert heading.find('.//'+W+'outlineLvl').get(W+'val')=='0'
            assert heading.find('.//'+W+'b') is not None
            page=doc.find('.//'+W+'pgSz');assert page.get(W+'w')=='11906' and page.get(W+'h')=='16838'
        preview=next(e['render'] for e in runner.events if e['type']=='candidate' and e.get('render') and e['revision']==candidate['candidate_hash'])
        pdf=PdfReader(io.BytesIO(store.blob(preview['pdf_hash'])))
        assert len(pdf.pages)==2
        toc=pdf.pages[0].extract_text().strip();assert 'Findings' in toc and toc.endswith('2')
        assert 'requires a field update' not in toc
        assert preview['field_update']['indexes_updated']==1
        assert 'The pilot is ready for review.' in pdf.pages[1].extract_text()
        assert not (root/'report.docx').exists()
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        restore=store.undo(undo['id'],'restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (root/'report.docx').read_bytes()==data
        report.update(status='passed',revision=candidate['candidate_hash'],render=preview,exact_restore=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        if task:
            report['metrics']=summary(store,task)
            pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone()
            report['selected_skill']=dict(pinned) if pinned else None
        report['events']=runner.events
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-contents-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','events','model')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
