"""Render a synthetic Word character-style creation/assignment fixture for explicit visual inspection."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_tables import saved_table as character_fixture
from test_word_character_assignment import create, assign
from test_word_text_format import get
from test_word_styles import style_read
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-independent-character-review-',dir='.runtime')).resolve()
    store,plugin,original,revision=character_fixture(root)
    try:
        target=style_read(plugin,revision)
        created=create(plugin,revision,target,inherit_base=False)
        current=created['candidate_revision']
        result=assign(plugin,current,get(plugin,current))
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=store.blob(rendered['pdf_hash']);(root/'preview.pdf').write_bytes(pdf)
        document=pdfium.PdfDocument(pdf);images=[]
        for i in range(len(document)):
            page=document[i];bitmap=page.render(scale=1.25);image=root/f'page-{i+1}.png';bitmap.to_pil().save(image)
            images.append(str(image));bitmap.close();page.close()
        document.close()
        report={'status':'rendered_pending_inspection','project':str(root),'revision':result['candidate_revision'],
                'pdf_hash':rendered['pdf_hash'],'pages':rendered['pages'],'images':images,
                'original_unchanged':(root/'report.docx').read_bytes()==original}
        Path('docs/benchmarks/word-independent-character-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
