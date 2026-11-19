"""Render a synthetic Word inline-image fixture for explicit visual inspection."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer
from localoffice.assets import AssetPlugin
from PIL import Image,ImageDraw

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-image-review-',dir='.runtime')).resolve()
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        created=plugin.create(**design(),idempotency_key='fixture');revision=created['candidate_revision']
        image=Image.new('RGB',(300,180),'#17365D');draw=ImageDraw.Draw(image);draw.rectangle((20,20,140,160),fill='#64C7BB');draw.ellipse((170,40,270,140),fill='#F0B35A');image.save(root/'figure.png')
        asset=AssetPlugin(store).import_image(path='figure.png')
        target=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:2'])['objects'][0]
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=[{
            'id':'image','op':'insert_image','target':target['id'],'expected_hash':target['hash'],
            'asset_id':asset['asset_id'],'width_inches':3,'alt_text':'Teal rectangle and gold circle on a navy background','caption':'Figure 1 — Imported editable image'}],idempotency_key='layout')
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=store.blob(rendered['pdf_hash']);(root/'preview.pdf').write_bytes(pdf)
        document=pdfium.PdfDocument(pdf);images=[]
        for i in range(len(document)):
            page=document[i];bitmap=page.render(scale=1.25);image=root/f'page-{i+1}.png';bitmap.to_pil().save(image)
            images.append(str(image));bitmap.close();page.close()
        document.close()
        report={'status':'rendered_pending_inspection','project':str(root),'revision':result['candidate_revision'],
                'pdf_hash':rendered['pdf_hash'],'pages':rendered['pages'],'images':images,
                'original_absent':not (root/'report.docx').exists()}
        Path('docs/benchmarks/word-image-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
