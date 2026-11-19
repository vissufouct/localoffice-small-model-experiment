"""Render a synthetic Word cell formatting fixture for explicit visual inspection."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_tables import saved_table
from test_word_tables import get
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-cell-format-review-',dir='.runtime')).resolve()
    store,plugin,original,revision=saved_table(root)
    try:
        operations=[]
        for target in ['cell:1','cell:2']:
            obj=get(plugin,revision,target)
            operations.append({'op':'format_cell','target':target,'expected_hash':obj['hash'],'properties':{'fill':'DBEAFE','vertical_alignment':'center','padding_top_twips':160,'padding_bottom_twips':160,'padding_left_twips':120}})
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=operations,idempotency_key='visual-cells')
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
        Path('docs/benchmarks/word-cell-format-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
