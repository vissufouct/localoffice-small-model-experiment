import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from localoffice.word.contracts import CreateDocument
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer
from test_word_creation import W


@pytest.mark.asyncio
@pytest.mark.parametrize('next_kind',['paragraph','table'])
async def test_explicit_break_renders_next_content_on_second_page(tmp_path,next_kind):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    following={'kind':'paragraph','text':'Second page'} if next_kind=='paragraph' else {'kind':'table','rows':[['Second page']]}
    args={'path':'report.docx','blocks':[{'kind':'paragraph','text':'First page'},{'kind':'page_break'},following]}
    try:
        result=plugin.create(**args,idempotency_key='break')
        assert plugin.create(**args,idempotency_key='break')==result
        data=store.blob(result['candidate_revision'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root=ET.fromstring(archive.read('word/document.xml'));body=root.find('{'+W+'}body')
            paragraphs=body.findall('{'+W+'}p')
            assert len(paragraphs)==2
            assert [''.join(p.itertext()) for p in paragraphs]==['First page','Second page' if next_kind=='paragraph' else '']
            assert paragraphs[1].find('.//{'+W+'}pageBreakBefore') is not None
            if next_kind=='table':
                assert paragraphs[1].find('.//{'+W+'}keepNext') is not None
                assert body.find('{'+W+'}tbl') is not None
        render=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=PdfReader(io.BytesIO(store.blob(render['pdf_hash'])))
        assert len(pdf.pages)==2
        assert 'First page' in pdf.pages[0].extract_text() and 'Second page' not in pdf.pages[0].extract_text()
        assert 'Second page' in pdf.pages[1].extract_text() and 'First page' not in pdf.pages[1].extract_text()
        assert not (tmp_path/'report.docx').exists()
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='save')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        restore=store.undo(undo['id'],'restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==data
    finally:store.close()


@pytest.mark.parametrize('blocks',[
    [{'kind':'page_break'}],
    [{'kind':'page_break'},{'kind':'paragraph','text':'x'}],
    [{'kind':'paragraph','text':'x'},{'kind':'page_break'}],
    [{'kind':'paragraph','text':'x'},{'kind':'page_break'},{'kind':'page_break'},{'kind':'paragraph','text':'y'}],
    [{'kind':'paragraph','text':'x'},{'kind':'page_break','text':'hidden'},{'kind':'paragraph','text':'y'}],
])
def test_explicit_break_rejects_ambiguous_positions_and_extra_content(blocks):
    with pytest.raises(ValidationError):CreateDocument(path='report.docx',blocks=blocks)
