import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import CreateDocument
from localoffice.word.render import WordRenderer
from test_word_creation import design,W


def band_design():
    args=design()
    args['styles'].append({'id':'Band','font':'Arial','size_half_points':18,'color':'17365D'})
    args['header']={'text':'Quarterly report','style':'Band','alignment':'right'}
    args['footer']={'text':'Page ','style':'Band','alignment':'center','page_numbers':'current_total','separator':' of '}
    args['page'].update(header_distance_twips=360,footer_distance_twips=400)
    return args


def test_create_bands_relationships_fields_staging_replay_and_exact_restore(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        args=band_design();result=plugin.create(**args,idempotency_key='bands')
        assert not (tmp_path/'report.docx').exists()
        assert plugin.create(**args,idempotency_key='bands')==result
        original=store.blob(result['candidate_revision'])
        with zipfile.ZipFile(io.BytesIO(original)) as package:
            rels=ET.fromstring(package.read('word/_rels/document.xml.rels'))
            mapping={r.get('Id'):r for r in rels}
            document=ET.fromstring(package.read('word/document.xml'));section=document.find('.//{'+W+'}sectPr')
            for name in ['header','footer']:
                ref=section.find('{'+W+'}'+name+'Reference')
                assert ref.get('{'+W+'}type')=='default'
                rel=mapping[ref.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
                assert rel.get('Type').endswith('/'+name)
                target=rel.get('Target').lstrip('/')
                if not target.startswith('word/'):target='word/'+target
                band=ET.fromstring(package.read(target))
                assert band.find('.//{'+W+'}pStyle').get('{'+W+'}val')=='Band'
                if name=='footer':
                    fields=band.findall('.//{'+W+'}fldSimple')
                    assert [f.get('{'+W+'}instr').strip() for f in fields]==['PAGE','NUMPAGES']
                    assert all(f.get('{'+W+'}dirty') in ('true','1') for f in fields)
                    assert band.find('.//{'+W+'}jc').get('{'+W+'}val')=='center'
                else:assert ''.join(band.itertext())=='Quarterly report'
            margin=section.find('{'+W+'}pgMar')
            assert margin.get('{'+W+'}header')=='360' and margin.get('{'+W+'}footer')=='400'
            settings=ET.fromstring(package.read('word/settings.xml'))
            assert settings.find('{'+W+'}updateFields').get('{'+W+'}val') in ('true','1')
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='save')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        assert not (tmp_path/'report.docx').exists()
        restore=store.undo(undo['id'],'restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.asyncio
async def test_render_updates_page_and_total_fields_on_each_page(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        result=plugin.create(**band_design(),idempotency_key='bands')
        render=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        pdf=PdfReader(io.BytesIO(store.blob(render['pdf_hash'])))
        assert len(pdf.pages)==2
        for i,page in enumerate(pdf.pages,1):
            text=' '.join(page.extract_text().split())
            assert 'Quarterly report' in text
            assert f'Page {i} of 2' in text,text
        assert not (tmp_path/'report.docx').exists()
    finally:store.close()


@pytest.mark.parametrize('patch',[
    {'header':{'style':'Missing'}}, {'footer':{'text':'bad\x01'}},
    {'footer':{'page_numbers':'arbitrary_field'}}, {'header':{'text':'x'*2001}},
    {'footer':{'separator':'x'*41}}, {'header':{'text':'\ud800'}},
    {'page':{'header_distance_twips':-1}},
])
def test_band_contract_rejects_unknown_styles_invalid_text_and_bounds(patch):
    with pytest.raises(ValidationError):CreateDocument(**{**design(),**patch})
