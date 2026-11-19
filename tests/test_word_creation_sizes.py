import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import WordStyle
from test_word_creation import W


@pytest.mark.parametrize('size,expected',[({},22),({'size_points':9},18),({'size_points':11.5},23),({'size_points':144},288),({'size_half_points':18},18)])
def test_creation_resolves_requested_point_sizes_in_body_and_bands(tmp_path,size,expected):
    store=ProjectStore(tmp_path)
    try:
        result=WordPlugin(store).create(path='report.docx',styles=[{'id':'Shared',**size}],blocks=[{'kind':'paragraph','style':'Shared','text':'Body'}],header={'style':'Shared','text':'Header'},footer={'style':'Shared','page_numbers':'current'},idempotency_key='size')
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as archive:
            styles=ET.fromstring(archive.read('word/styles.xml'))
            style=next(s for s in styles if s.get('{'+W+'}styleId')=='Shared')
            assert style.find('.//{'+W+'}sz').get('{'+W+'}val')==str(expected)
            for part in ['word/document.xml','word/header1.xml','word/footer1.xml']:
                # SDK names can vary; select the corresponding generated part.
                stem=part.rsplit('/',1)[1].split('1.')[0]
                names=[n for n in archive.namelist() if n.startswith('word/'+stem) and n.endswith('.xml')]
                assert len(names)==1
                root=ET.fromstring(archive.read(names[0]))
                assert root.find('.//{'+W+'}pStyle').get('{'+W+'}val')=='Shared'
        assert not (tmp_path/'report.docx').exists()
    finally:store.close()


@pytest.mark.parametrize('size',[{'size_points':5.5},{'size_points':144.5},{'size_points':9.25},{'size_points':float('nan')},{'size_points':9,'size_half_points':18},{'size_half_points':9}])
def test_creation_rejects_invalid_or_ambiguous_font_units(size):
    with pytest.raises(ValidationError):WordStyle(id='Body',**size)
