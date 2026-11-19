"""Render four pages exercising first/even/default native bands; review separately."""
import asyncio
import json
import tempfile
from pathlib import Path
import pypdfium2 as pdfium
from pypdf import PdfReader
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer


async def main():
    root=Path(tempfile.mkdtemp(prefix='word-band-variants-',dir='.runtime')).resolve()
    out=Path('docs/benchmarks/word-band-variants-visual');out.mkdir(exist_ok=True)
    store=ProjectStore(root)
    try:
        args={'path':'report.docx','styles':[{'id':'Body','font':'Arial','size_points':11},
              {'id':'Band','font':'Arial','size_points':9}],
              'blocks':[{'kind':'paragraph','text':f'Body page {n}','style':'Body','page_break_before':n>1} for n in range(1,5)]}
        for prefix,label in [('', 'Default'),('first_','First'),('even_','Even')]:
            args[prefix+'header']={'text':label+' header','style':'Band','alignment':'right'}
            args[prefix+'footer']={'text':label+' page ','style':'Band','alignment':'center','page_numbers':'current_total','separator':' of '}
        result=WordPlugin(store).create(**args,idempotency_key='variants')
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=store.blob(rendered['pdf_hash']);(out/'preview.pdf').write_bytes(pdf)
        (out/'report.docx').write_bytes(store.blob(result['candidate_revision']))
        reader=PdfReader(out/'preview.pdf');assert len(reader.pages)==4
        texts=[' '.join(page.extract_text().split()) for page in reader.pages]
        for n,(text,label) in enumerate(zip(texts,['First','Even','Default','Even']),1):
            assert label+' header' in text and f'{label} page {n} of 4' in text and f'Body page {n}' in text,text
            assert all(other+' header' not in text for other in ['First','Even','Default'] if other!=label),text
        document=pdfium.PdfDocument(pdf)
        for i in range(len(document)):
            page=document[i];bitmap=page.render(scale=1.15)
            bitmap.to_pil().save(out/f'page-{i+1}.png');bitmap.close();page.close()
        document.close()
        report={'project':str(root),'revision':result['candidate_revision'],'pdf_hash':rendered['pdf_hash'],
                'pages':4,'page_texts':texts,'text_checks':'passed','review_status':'pending','reviewer':None,
                'scope':'Registered deterministic creation and LibreOffice render, not real-model or Microsoft Word acceptance.'}
        (out/'evidence.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
