"""Render a synthetic Word style-rename fixture for explicit visual inspection."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-style-rename-review-',dir='.runtime')).resolve()
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        created=plugin.create(**design(),idempotency_key='fixture');revision=created['candidate_revision']
        target=plugin.read_objects(path='report.docx',revision=revision,ids=['style:Body'])['objects'][0]
        before=await WordRenderer(store).render(path='report.docx',revision=revision)
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=[{
            'op':'rename_style','target':target['id'],'expected_hash':target['hash'],
            'name':'Report body'}],idempotency_key='rename')
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=store.blob(rendered['pdf_hash']);(root/'preview.pdf').write_bytes(pdf)
        document=pdfium.PdfDocument(pdf);baseline=pdfium.PdfDocument(store.blob(before['pdf_hash']));images=[]
        assert len(document)==len(baseline)
        for i in range(len(document)):
            page=document[i];bitmap=page.render(scale=1.25);image=root/f'page-{i+1}.png';bitmap.to_pil().save(image)
            baseline_page=baseline[i];baseline_bitmap=baseline_page.render(scale=1.25)
            assert bitmap.to_pil().tobytes()==baseline_bitmap.to_pil().tobytes(),f'Page {i+1} changed visually'
            baseline_bitmap.close();baseline_page.close()
            images.append(str(image));bitmap.close();page.close()
        document.close();baseline.close()
        report={'status':'rendered_pending_inspection','project':str(root),'revision':result['candidate_revision'],
                'baseline_revision':revision,'baseline_pdf_hash':before['pdf_hash'],'all_page_pixels_unchanged':True,'pdf_hash':rendered['pdf_hash'],'pages':rendered['pages'],'images':images,
                'original_absent':not (root/'report.docx').exists()}
        Path('docs/benchmarks/word-style-rename-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
