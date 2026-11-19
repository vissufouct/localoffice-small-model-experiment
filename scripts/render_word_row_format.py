"""Render a two-page table to inspect repeat headers and minimum row heights."""
import asyncio,json,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer

async def main():
    root=Path(tempfile.mkdtemp(prefix='word-row-format-review-',dir='.runtime')).resolve()
    store=ProjectStore(root);plugin=WordPlugin(store)
    try:
        spec=design();spec['page']={'width_twips':11906,'height_twips':16838,'margin_twips':1134}
        spec['blocks']=[{'kind':'paragraph','text':'Project cost summary','style':'Title'},
            {'kind':'table','rows':[['Item','Budget']]+[[f'Activity {i:02}',f'EUR {i*100}'] for i in range(1,20)],'header_row':False}]
        created=plugin.create(**spec,idempotency_key='fixture');store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
        original=(root/'report.docx').read_bytes();revision=created['candidate_revision']
        rows=plugin.list_objects(path='report.docx',revision=revision,kind='row',limit=20)['objects'];operations=[]
        for i,row in enumerate(rows):
            properties={'height':{'twips':900,'rule':'at_least'},'allow_split':False}
            if i==0:properties['repeat_header']=True
            operations.append({'op':'format_row','target':row['id'],'expected_hash':row['hash'],'properties':properties})
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=operations,idempotency_key='row-design')
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=store.blob(rendered['pdf_hash']);(root/'preview.pdf').write_bytes(pdf)
        document=pdfium.PdfDocument(pdf);images=[];texts=[]
        for i in range(len(document)):
            page=document[i];textpage=page.get_textpage();texts.append(textpage.get_text_range());textpage.close()
            bitmap=page.render(scale=1.1);image=root/f'page-{i+1}.png';bitmap.to_pil().save(image);images.append(str(image));bitmap.close();page.close()
        document.close()
        assert len(texts)==2,texts
        assert all('Item' in text and 'Budget' in text for text in texts)
        joined='\n'.join(texts)
        assert all(joined.count(f'Activity {i:02}')==1 for i in range(1,20))
        report={'status':'rendered_pending_inspection','project':str(root),'revision':result['candidate_revision'],'pdf_hash':rendered['pdf_hash'],'pages':rendered['pages'],'images':images,'header_text_on_each_page':True,'all_19_body_rows_once':True,'original_unchanged':(root/'report.docx').read_bytes()==original}
        Path('docs/benchmarks/word-row-format-render.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
