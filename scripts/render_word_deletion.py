"""Render before/after paragraph removal; preserve exact revision evidence."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-deletion-review-',dir='.runtime')).resolve()
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        created=plugin.create(**design(),idempotency_key='fixture');revision=created['candidate_revision']
        store.apply(created['receipt_id'],expected_candidate=revision,action='save')
        original=(root/'report.docx').read_bytes()
        before=await WordRenderer(store).render(path='report.docx',revision=revision)
        obj=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:2'])['objects'][0]
        removed=plugin.stage_batch(path='report.docx',revision=revision,operations=[{
            'op':'delete_paragraph','target':obj['id'],'expected_hash':obj['hash']}],idempotency_key='delete')
        after=await WordRenderer(store).render(path='report.docx',revision=removed['candidate_revision'])
        assert (root/'report.docx').read_bytes()==original
        renders=[]
        for label,result in [('before',before),('after',after)]:
            pdf=store.blob(result['pdf_hash']);(root/(label+'.pdf')).write_bytes(pdf)
            document=pdfium.PdfDocument(pdf);images=[]
            for i in range(len(document)):
                page=document[i];bitmap=page.render(scale=1.1);image=root/f'{label}-{i+1}.png'
                bitmap.to_pil().save(image);images.append(str(image));bitmap.close();page.close()
            document.close();renders.append({'label':label,'revision':result['revision'],'pdf_hash':result['pdf_hash'],'pages':result['pages'],'images':images})
        store.apply(removed['receipt_id'],expected_candidate=removed['candidate_revision'],action='apply')
        undo=store.undo(removed['receipt_id'],'undo-delete');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        report={'status':'rendered_pending_inspection','project':str(root),'renders':renders,'deleted_text':obj['text'],'receipt':removed,'exact_undo':True}
        Path('docs/benchmarks/word-deletion-render.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
