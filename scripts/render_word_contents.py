"""Render a synthetic Word native table-of-contents fixture for explicit visual inspection."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_contents import toc_design
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-contents-review-',dir='.runtime')).resolve()
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        result=plugin.create(**toc_design(),idempotency_key='visual-toc')
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=store.blob(rendered['pdf_hash']);(root/'preview.pdf').write_bytes(pdf)
        document=pdfium.PdfDocument(pdf);images=[]
        for i in range(len(document)):
            page=document[i];bitmap=page.render(scale=1.25);image=root/f'page-{i+1}.png';bitmap.to_pil().save(image)
            images.append(str(image));bitmap.close();page.close()
        document.close()
        report={'status':'rendered_pending_inspection','project':str(root),'revision':result['candidate_revision'],
                'pdf_hash':rendered['pdf_hash'],'pages':rendered['pages'],'images':images,
                'warnings':rendered['warnings'],'field_update':rendered.get('field_update'),
                'toc_pdf_update_qualified':bool(rendered.get('field_update')),'docx_cache_update_qualified':False,
                'source_not_created':not (root/'report.docx').exists()}
        Path('docs/benchmarks/word-contents-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
