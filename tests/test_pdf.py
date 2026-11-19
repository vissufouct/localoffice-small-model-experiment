import asyncio
import shutil
from pathlib import Path
import pytest
from localoffice.storage import ProjectStore,digest
from localoffice.pdf import PdfPlugin
from localoffice.errors import HarnessError

@pytest.fixture
def pdf(tmp_path):
    for p in Path('tests/fixtures/pdf').glob('*.pdf'):shutil.copyfile(p,tmp_path/p.name)
    store=ProjectStore(tmp_path)
    yield PdfPlugin(store)
    store.close()

@pytest.mark.asyncio
async def test_pdf_source_provenance_and_render(pdf):
    original=(pdf.store.root/'text.pdf').read_bytes()
    result=await pdf.read('text.pdf',page=1,count=2)
    assert result['source_hash']==digest(original)
    assert result['page_count']==2
    assert 'Budget: EUR 120' in result['text']
    assert '20 November 2026' in result['text']
    assert result['pages'][1]['page']==2
    assert all(p['extraction']=='embedded' for p in result['pages'])
    view=await pdf.job('text.pdf',command='render',page=1)
    assert view['visual_reviewed'] is False
    assert pdf.store.blob(view['image_hash']).startswith(b'\x89PNG')
    assert (pdf.store.root/'text.pdf').read_bytes()==original

@pytest.mark.asyncio
async def test_scanned_pages_require_explicit_ocr(pdf):
    result=await pdf.read('scan-english.pdf')
    assert result['pages'][0]['extraction']=='no_text'
    result=await pdf.read('scan-english.pdf',ocr=True,languages='eng')
    assert result['pages'][0]['extraction']=='ocr'
    assert 'Cedar' in result['text'] and '120' in result['text']

@pytest.mark.asyncio
async def test_portuguese_mixed_pdf(pdf):
    result=await pdf.read('mixed.pdf',count=2,ocr=True,languages='eng+por')
    assert [p['extraction'] for p in result['pages']]==['embedded','ocr']
    assert 'Cedro' in result['pages'][1]['text']
    assert '120 euros' in result['pages'][1]['text']

@pytest.mark.asyncio
async def test_malformed_encrypted_and_page_errors(pdf):
    for name,code in [('malformed.pdf','pdf_invalid'),('encrypted.pdf','pdf_encrypted')]:
        with pytest.raises(HarnessError) as exc:await pdf.read(name)
        assert exc.value.code==code
    result=await pdf.job('encrypted.pdf',password='fixture-password')
    assert 'Cedar' in result['text']
    assert 'fixture-password' not in str(result)
    with pytest.raises(HarnessError) as exc:await pdf.read('text.pdf',page=99)
    assert exc.value.code=='page_out_of_range'

@pytest.mark.asyncio
async def test_pdf_cancellation_settles_owned_worker(pdf):
    job=asyncio.create_task(pdf.read('mixed.pdf',count=2,ocr=True))
    for _ in range(100):
        if pdf.process:break
        await asyncio.sleep(.01)
    assert pdf.process is not None
    process=pdf.process
    job.cancel()
    with pytest.raises(asyncio.CancelledError):await job
    assert process.returncode is not None
    assert not pdf.busy and pdf.process is None


@pytest.mark.asyncio
async def test_extraction_rejects_preview_revision_after_external_change(pdf):
    old=digest((pdf.store.root/'text.pdf').read_bytes())
    (pdf.store.root/'text.pdf').write_bytes((pdf.store.root/'mixed.pdf').read_bytes())
    with pytest.raises(HarnessError) as exc:
        await pdf.job('text.pdf',expected_revision=old)
    assert exc.value.code=='stale_revision'
    assert not pdf.busy


@pytest.mark.asyncio
async def test_explicit_pdf_cancel_returns_controlled_error(pdf):
    job=asyncio.create_task(pdf.read('mixed.pdf',count=2,ocr=True))
    for _ in range(100):
        if pdf.process:break
        await asyncio.sleep(.001)
    process=pdf.process
    assert process is not None
    assert (await pdf.cancel())['status']=='cancelling'
    with pytest.raises(HarnessError) as error:await job
    assert error.value.code=='pdf_cancelled'
    assert process.returncode is not None and not pdf.busy
    assert (await pdf.cancel())['status']=='idle'
    assert 'Cedar' in (await pdf.read('text.pdf'))['text']
