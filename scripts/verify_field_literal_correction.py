"""Deterministic registered-tool correction of a retained failed candidate; not model acceptance."""
import argparse,asyncio,json,hashlib
from pathlib import Path
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer
from localoffice.word.creation_evidence import inspect

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);a=parser.parse_args()
    a.output.mkdir(parents=True,exist_ok=False);original=a.source.read_bytes();(a.output/'report.docx').write_bytes(original)
    store=ProjectStore(a.output);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('report.docx')
        matches=plugin.find_text(path='report.docx',revision=revision,query='Page of',scope='footers')['matches']
        assert len(matches)==1;match=matches[0]
        prefix=plugin.replace_text(path='report.docx',revision=revision,target=match['id'],expected_hash=match['hash'],start=match['start'],end=match['end'],text='Page ',idempotency_key='prefix')
        current=prefix['candidate_revision'];obj=plugin.read_objects(path='report.docx',revision=current,ids=[match['id']])['objects'][0]
        aligned=plugin.stage_batch(path='report.docx',revision=current,operations=[{'op':'format_paragraph','target':obj['id'],'expected_hash':obj['hash'],'properties':{'alignment':'center'}}],idempotency_key='align')
        current=aligned['candidate_revision'];render=await WordRenderer(store).render(path='report.docx',revision=current)
        pdfbytes=store.blob(render['pdf_hash']);(a.output/'corrected.pdf').write_bytes(pdfbytes);(a.output/'corrected.docx').write_bytes(store.blob(current))
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        import io
        pdf=PdfReader(io.BytesIO(pdfbytes));assert len(pdf.pages)==2
        for i,page in enumerate(pdf.pages,1):assert f'Page {i} of 2' in ' '.join(page.extract_text().split())
        images=[];doc=pdfium.PdfDocument(pdfbytes)
        for i in range(len(doc)):
            page=doc[i];bitmap=page.render(scale=1.25);image=a.output/f'page-{i+1}.png';bitmap.to_pil().save(image);images.append(str(image.resolve()));bitmap.close();page.close()
        doc.close()
        assert (a.output/'report.docx').read_bytes()==original
        full=inspect(store.blob(current));assert full['body_top_level_counts']['paragraph']==2
        store.apply(aligned['receipt_id'],expected_candidate=current,action='apply')
        undo=store.undo(aligned['receipt_id'],'undo-align');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (a.output/'report.docx').read_bytes()==original
        result={'scope':'deterministic registered tools, not local-model acceptance','source_hash':hashlib.sha256(original).hexdigest(),'revision':current,'pdf_hash':render['pdf_hash'],'images':images,'exact_undo':True,'pages_and_field_values_verified':True,'visual_review':'pending'}
        (a.output/'evidence.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    finally:store.close()
if __name__=='__main__':asyncio.run(main())
