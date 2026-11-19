import io
import zipfile
import pytest
from pypdf import PdfReader
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer,preflight
from localoffice.errors import HarnessError
from test_word_creation import design


@pytest.mark.asyncio
async def test_render_revision_cache_and_source_immutability(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store);renderer=WordRenderer(store)
    try:
        receipt=plugin.create(**design(),idempotency_key='create')
        revision=receipt['candidate_revision'];original=store.blob(revision)
        result=await renderer.render(path='report.docx',revision=revision)
        assert result['pages']==2 and not result['cached']
        pdf=PdfReader(io.BytesIO(store.blob(result['pdf_hash'])))
        assert 'Original report design' in pdf.pages[0].extract_text()
        assert 'EUR 120' in pdf.pages[0].extract_text()
        assert 'Next page' in pdf.pages[1].extract_text()
        cached=await renderer.render(path='report.docx',revision=revision)
        assert cached['cached'] and cached['pdf_hash']==result['pdf_hash']
        assert store.blob(revision)==original and not (tmp_path/'report.docx').exists()
        with pytest.raises(HarnessError,match='revision'):
            await renderer.render(path='report.docx',revision='0'*64)
    finally:store.close()


def test_render_blocks_external_resources():
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('word/_rels/document.xml.rels','<Relationships><Relationship TargetMode="External" Type="image" Target="https://invalid.example/image.png"/></Relationships>')
    with pytest.raises(HarnessError,match='external'):
        preflight(buf.getvalue())
