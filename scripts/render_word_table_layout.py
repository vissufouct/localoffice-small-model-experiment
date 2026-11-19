"""Render a synthetic Word table layout fixture for explicit visual inspection."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-table-layout-review-',dir='.runtime')).resolve()
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        created=plugin.create(**design(),idempotency_key='fixture');revision=created['candidate_revision']
        target=plugin.read_objects(path='report.docx',revision=revision,ids=['table:1'])['objects'][0]
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=[{
            'id':'table-layout','op':'format_table','target':target['id'],'expected_hash':target['hash'],
            'properties':{'column_widths':{'unit':'in','values':[3,2]},'alignment':'center',
                          'padding_top_twips':120,'padding_bottom_twips':120,
                          'borders':{'style':'single','color':'17365D','size_eighth_points':8}}}],idempotency_key='layout')
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
        Path('docs/benchmarks/word-table-layout-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
