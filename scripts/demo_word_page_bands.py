"""Real MLX creation and batch rendering; isolated synthetic fixture, no downloads."""
import asyncio
import os
import io
import json
from pathlib import Path
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main(native=False,sampling_profile="low_temperature",source_planning=False,creation_review=False):
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-page-bands-'))
    store=ProjectStore(root);provider=MLXProvider(structural_decoding=not native and os.environ.get("LOCALOFFICE_TEST_STRUCTURAL_DECODING")=="1",native_tool_protocol=native,native_constrained=native,sampling_profile=sampling_profile);runner=TaskRunner(store,provider,source_planning=source_planning,creation_review=creation_review)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','model':model,'structural_decoding':provider.structural_decoding,'native_constrained':native,'source_planning':source_planning,'creation_review':creation_review,'sampling':provider.sampling,'context':4096,'project':str(root)};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        prompt='Create report.docx with exactly two body paragraphs: First page and Second page. Put a page break before Second page. Add a right-aligned header Quarterly report and a centered footer showing Page followed by the current page number, of, and total pages, using real page-number fields. Use A4 portrait, Arial 11 point body and 9 point header/footer. Stage the document and finish.'
        task=await runner.start(prompt,artifact='report.docx')
        await asyncio.wait_for(runner.job,150)
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone()
        report['selected_skill']=dict(pinned) if pinned else None
        report['metrics']=summary(store,task);report['events']=runner.events
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        assert state=='completed',state
        assert not any(e['type']=='error' for e in runner.events)
        candidate=store.candidate('report.docx');assert candidate
        assert not (root/'report.docx').exists()
        W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        with zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as archive:
            doc=ET.fromstring(archive.read('word/document.xml'));styles=ET.fromstring(archive.read('word/styles.xml'))
            paragraphs=doc.findall('.//'+W+'body/'+W+'p')
            assert [''.join(p.itertext()) for p in paragraphs]==['First page','Second page'], 'Body text does not match the requested exact paragraphs'
            assert paragraphs[1].find('.//'+W+'pageBreakBefore') is not None
            assert doc.find('.//'+W+'headerReference') is not None and doc.find('.//'+W+'footerReference') is not None
            stylemap={s.get(W+'styleId'):s for s in styles.findall(W+'style')}
            def check_style(p,size):
                style=stylemap[p.find(W+'pPr/'+W+'pStyle').get(W+'val')]
                assert style.find('.//'+W+'sz').get(W+'val')==size
                assert style.find('.//'+W+'rFonts').get(W+'ascii')=='Arial'
            for p in paragraphs:check_style(p,'22')
            for kind,align in [('header','right'),('footer','center')]:
                names=[n for n in archive.namelist() if n.startswith('word/'+kind) and n.endswith('.xml')]
                assert len(names)==1
                band=ET.fromstring(archive.read(names[0]));p=band.find(W+'p')
                check_style(p,'18');assert p.find('.//'+W+'jc').get(W+'val')==align
            size=doc.find('.//'+W+'pgSz')
            assert size.get(W+'w')=='11906' and size.get(W+'h')=='16838'

        previews=[e for e in runner.events if e['type']=='candidate' and e.get('render')]
        assert len(previews)==1 and previews[0]['revision']==candidate['candidate_hash']
        from pypdf import PdfReader
        pdf=PdfReader(io.BytesIO(store.blob(previews[0]['render']['pdf_hash'])))
        assert len(pdf.pages)==2
        for i,page in enumerate(pdf.pages,1):
            text=' '.join(page.extract_text().split())
            assert 'Quarterly report' in text and f'Page {i} of 2' in text,text
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        saved=(root/'report.docx').read_bytes()
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        restore=store.undo(undo['id'],'restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (root/'report.docx').read_bytes()==saved
        report.update(status='passed',rendered_page_fields_verified=True,exact_restore=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-page-bands-native-'+str(time.time_ns())+'.json' if native else 'docs/benchmarks/word-page-bands-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--native',action='store_true');parser.add_argument('--source-planning',action='store_true');parser.add_argument('--creation-review',action='store_true');parser.add_argument('--sampling-profile',default='low_temperature',choices=['low_temperature','qwen_recommended']);args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.native,args.sampling_profile,args.source_planning,args.creation_review)) else 1)
