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

async def main():
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-create-'))
    store=ProjectStore(root);provider=MLXProvider(structural_decoding=os.environ.get("LOCALOFFICE_TEST_STRUCTURAL_DECODING")=="1");runner=TaskRunner(store,provider)
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    report={'status':'started','model':model,'structural_decoding':provider.structural_decoding,'context':4096,'project':str(root)};started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        prompt='Create native-live-brief.docx with exactly two paragraphs: a bold blue title “Native live preview” and body “This synthetic report demonstrates a local working copy.” Use Arial, a 20-point title and 11-point body, portrait A4 with generous margins. Stage it with word.create, then ask me whether to add a conclusion so I can inspect and annotate the preview before continuing.'
        task=await runner.start(prompt,artifact='native-live-brief.docx')
        await asyncio.wait_for(runner.job,150)
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone()
        report['selected_skill']=dict(pinned) if pinned else None
        report['metrics']=summary(store,task);report['events']=runner.events
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        assert state=='paused',state
        questions=[e for e in runner.events if e['type']=='question']
        assert len(questions)==1 and 'conclusion' in questions[0]['text'].lower(),questions
        assert not any(e['type']=='error' for e in runner.events)
        candidate=store.candidate('native-live-brief.docx');assert candidate
        assert not (root/'native-live-brief.docx').exists()
        W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        with zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as archive:
            doc=ET.fromstring(archive.read('word/document.xml'));styles=ET.fromstring(archive.read('word/styles.xml'))
            paragraphs=doc.findall('.//'+W+'body/'+W+'p')
            assert [''.join(p.itertext()) for p in paragraphs]==['Native live preview','This synthetic report demonstrates a local working copy.']
            stylemap={s.get(W+'styleId'):s for s in styles.findall(W+'style')}
            for paragraph,size in zip(paragraphs,['40','22']):
                style=stylemap[paragraph.find(W+'pPr/'+W+'pStyle').get(W+'val')]
                assert style.find('.//'+W+'sz').get(W+'val')==size
                assert style.find('.//'+W+'rFonts').get(W+'ascii')=='Arial'
            title=stylemap[paragraphs[0].find(W+'pPr/'+W+'pStyle').get(W+'val')]
            assert title.find('.//'+W+'b') is not None
            report['title_color']=title.find('.//'+W+'color').get(W+'val')
        previews=[e for e in runner.events if e['type']=='candidate' and e.get('render')]
        assert len(previews)==1 and previews[0]['revision']==candidate['candidate_hash']
        assert store.blob(previews[0]['render']['pdf_hash']).startswith(b'%PDF-')
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        report.update(status='passed',original_absent=True,paragraphs_and_sizes_verified=True,render_before_question=True)
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();await provider.close();store.close();report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-create-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('metrics','model','events')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
