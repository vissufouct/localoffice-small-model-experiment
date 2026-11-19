import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import CreateDocument, BatchReceipt
from test_word_creation import design, W


def toc_design():
    args=design()
    args['blocks']=[{'kind':'table_of_contents','max_level':2},
                    {'kind':'page_break'},*args['blocks']]
    return args


def test_native_toc_staging_replay_and_exact_undo(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        args=toc_design();receipt=plugin.create(**args,idempotency_key='toc')
        assert BatchReceipt(**receipt).warnings
        assert plugin.create(**args,idempotency_key='toc')==receipt
        assert not (tmp_path/'report.docx').exists()
        data=store.blob(receipt['candidate_revision'])
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            doc=ET.fromstring(package.read('word/document.xml'))
            assert [f.text.strip() for f in doc.findall('.//{'+W+'}instrText')]==['TOC \\o "1-2" \\h']
            fields=doc.findall('.//{'+W+'}fldChar')
            assert [f.get('{'+W+'}fldCharType') for f in fields]==['begin','separate','end']
            assert fields[0].get('{'+W+'}dirty') in ('true','1')
            assert doc.find('.//{'+W+'}pageBreakBefore') is not None
            styles=ET.fromstring(package.read('word/styles.xml'))
            assert styles.find('.//{'+W+'}outlineLvl').get('{'+W+'}val')=='0'
            settings=ET.fromstring(package.read('word/settings.xml'))
            assert settings.find('{'+W+'}updateFields').get('{'+W+'}val') in ('true','1')
        store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='save')
        undo=store.undo(receipt['receipt_id'],'undo')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        assert not (tmp_path/'report.docx').exists()
        restore=store.undo(undo['id'],'restore')
        store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==data
    finally:store.close()


@pytest.mark.parametrize('blocks',[
    [{'kind':'table_of_contents','max_level':0}],
    [{'kind':'table_of_contents','max_level':7}],
    [{'kind':'table_of_contents'},{'kind':'table_of_contents'}],
    [{'kind':'table_of_contents'},{'kind':'paragraph','text':'No heading'}],
])
def test_toc_rejects_invalid_levels_duplicates_or_missing_heading(blocks):
    with pytest.raises(ValidationError):CreateDocument(**{**design(),'blocks':blocks})


def test_toc_requires_heading_within_requested_range():
    args=toc_design();args['styles'][0]['heading_level']=3
    with pytest.raises(ValidationError,match='in range'):CreateDocument(**args)


@pytest.mark.asyncio
async def test_toc_render_preserves_working_copy_and_warns_on_cache_hit(tmp_path):
    from localoffice.word.render import WordRenderer
    from pypdf import PdfReader
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        receipt=plugin.create(**toc_design(),idempotency_key='toc')
        before=store.blob(receipt['candidate_revision'])
        renderer=WordRenderer(store)
        first=await renderer.render(path='report.docx',revision=receipt['candidate_revision'])
        cached=await renderer.render(path='report.docx',revision=receipt['candidate_revision'])
        assert cached['cached'] and cached['warnings']==first['warnings']
        assert any('TOC' in warning for warning in first['warnings'])
        pdf=PdfReader(io.BytesIO(store.blob(first['pdf_hash'])))
        assert len(pdf.pages)==3
        assert 'requires a field update' not in pdf.pages[0].extract_text()
        assert 'Original report design' in pdf.pages[0].extract_text()
        assert pdf.pages[0].extract_text().strip().endswith('2')
        assert first['field_update']['indexes_updated']==1
        assert first['field_update']['update_passes']>=2
        assert 'Original report design' in pdf.pages[1].extract_text()
        assert store.blob(receipt['candidate_revision'])==before
        assert not (tmp_path/'report.docx').exists()
        match=plugin.find_text(path='report.docx',revision=receipt['candidate_revision'],query='Original report design')['matches'][0]
        edited=plugin.stage_batch(path='report.docx',revision=receipt['candidate_revision'],idempotency_key='rename-heading',operations=[{
            'id':'heading','op':'replace_text','target':match['id'],'expected_hash':match['hash'],
            'start':match['start'],'end':match['end'],'text':'Revised heading'}])
        newer=await renderer.render(path='report.docx',revision=edited['candidate_revision'])
        updated=PdfReader(io.BytesIO(store.blob(newer['pdf_hash'])))
        assert not newer['cached'] and newer['pdf_hash']!=first['pdf_hash']
        assert 'Revised heading' in updated.pages[0].extract_text()
        assert 'Original report design' not in updated.pages[0].extract_text()
        assert store.blob(receipt['candidate_revision'])==before
    finally:store.close()


@pytest.mark.parametrize('field,expected',[
    ('<w:fldSimple w:instr=" TOC &quot;1-3&quot; "/>',True),
    ('<w:fldChar w:fldCharType="begin"/><w:instrText> TO</w:instrText><w:instrText>C \\h </w:instrText><w:fldChar w:fldCharType="separate"/><w:fldChar w:fldCharType="end"/>',True),
    ('<w:fldSimple w:instr=" HYPERLINK &quot;TOC&quot; "/>',False),
    ('<w:t>TOC</w:t>',False),
])
def test_imported_field_warning_handles_split_codes_not_visible_text(field,expected):
    from localoffice.word.render import field_warnings
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as package:
        package.writestr('word/document.xml',f'<w:document xmlns:w="{W}"><w:body><w:p>{field}</w:p></w:body></w:document>')
    assert bool(field_warnings(stream.getvalue()))==expected


@pytest.mark.asyncio
async def test_toc_heading_levels_and_page_fields_are_updated_together(tmp_path):
    from localoffice.word.render import WordRenderer
    from pypdf import PdfReader
    store=ProjectStore(tmp_path)
    try:
        args={'path':'report.docx','styles':[
            {'id':'HeadingOne','heading_level':1},
            {'id':'HeadingTwo','heading_level':2},
            {'id':'HeadingThree','heading_level':3}],
            'footer':{'text':'Page ','page_numbers':'current_total','separator':' of '},
            'blocks':[{'kind':'table_of_contents','max_level':2},
                *[{'kind':'paragraph','style':style,'text':text,'page_break_before':True}
                  for style,text in [('HeadingOne','Overview'),('HeadingTwo','Details'),
                                     ('HeadingThree','Excluded depth'),('HeadingOne','Conclusion')]]]}
        receipt=WordPlugin(store).create(**args,idempotency_key='levels')
        render=await WordRenderer(store).render(path='report.docx',revision=receipt['candidate_revision'])
        pdf=PdfReader(io.BytesIO(store.blob(render['pdf_hash'])))
        assert len(pdf.pages)==5
        toc=pdf.pages[0].extract_text()
        import re
        for text,number in [('Overview',2),('Details',3),('Conclusion',5)]:
            assert re.search(re.escape(text)+r'[.\s]*'+str(number),toc),toc
        assert 'Excluded depth' not in toc
        for number,page in enumerate(pdf.pages,1):
            assert f'Page {number} of 5' in ' '.join(page.extract_text().split())
    finally:store.close()


def test_toc_adapter_rejects_nonbaseline_field_before_starting_process():
    from localoffice.word.fields import adapter
    from localoffice.errors import HarnessError
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as package:
        package.writestr('word/document.xml',f'<w:document xmlns:w="{W}"><w:fldSimple w:instr="INCLUDETEXT external.docx"/></w:document>')
    with pytest.raises(HarnessError,match='baseline'):adapter(stream.getvalue())


def long_toc_design():
    return {'path':'report.docx','page':{'width_twips':8000,'height_twips':6000,'margin_twips':500},
        'styles':[{'id':'Normal','size_points':18},{'id':'Heading','heading_level':1,'size_points':18}],
        'blocks':[{'kind':'table_of_contents'},*[{'kind':'paragraph','style':'Heading',
            'text':f'Chapter {i:02d}','page_break_before':True} for i in range(1,19)]]}


@pytest.mark.asyncio
async def test_toc_expansion_recalculates_every_destination_page(tmp_path):
    import re
    from pypdf import PdfReader
    from localoffice.word.render import WordRenderer
    store=ProjectStore(tmp_path)
    try:
        receipt=WordPlugin(store).create(**long_toc_design(),idempotency_key='long-toc')
        result=await WordRenderer(store).render(path='report.docx',revision=receipt['candidate_revision'])
        pages=PdfReader(io.BytesIO(store.blob(result['pdf_hash']))).pages
        texts=[page.extract_text().strip() for page in pages]
        first=texts.index('Chapter 01')
        assert first>=2,'Fixture must expand TOC beyond its initial single page'
        toc='\n'.join(texts[:first])
        for i in range(1,19):
            title=f'Chapter {i:02d}'
            actual=texts.index(title)+1
            assert re.search(re.escape(title)+r'[.\s]*'+str(actual)+r'\b',toc),(title,actual,toc)
        assert not (tmp_path/'report.docx').exists()
    finally:store.close()


@pytest.mark.asyncio
async def test_cancel_toc_render_cleans_up_owned_office_and_keeps_source(tmp_path):
    import asyncio,psutil
    from localoffice.word.render import WordRenderer
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path);task=None;renderer=WordRenderer(store)
    try:
        receipt=WordPlugin(store).create(**long_toc_design(),idempotency_key='cancel-toc')
        original=store.blob(receipt['candidate_revision'])
        task=asyncio.create_task(renderer.render(path='report.docx',revision=receipt['candidate_revision']))
        children=[]
        for _ in range(300):
            if renderer.process and renderer.process.returncode is None:
                children=[p for p in psutil.Process(renderer.process.pid).children(recursive=True)
                          if p.name()=='soffice']
                if children:break
            if task.done():break
            await asyncio.sleep(.01)
        assert children,'Must observe the actual owned Office process before cancelling'
        owned=[(p.pid,p.create_time()) for p in children]
        await renderer.cancel()
        with pytest.raises(HarnessError,match='stopped'):await task
        from localoffice.worker_guard import alive
        for _ in range(100):
            if not any(alive(pid,born) for pid,born in owned):break
            await asyncio.sleep(.02)
        assert not any(alive(pid,born) for pid,born in owned)
        assert not store.db.execute('SELECT 1 FROM render_cache').fetchone()
        assert store.blob(receipt['candidate_revision'])==original
    finally:
        if task and not task.done():
            await renderer.cancel()
            await asyncio.gather(task,return_exceptions=True)
        store.close()
