import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import CreateDocument, BatchReceipt
from localoffice.errors import HarnessError

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def design():
    return {'path':'report.docx','styles':[
        {'id':'Title','font':'Arial','size_half_points':36,'bold':True,'color':'17365D','heading_level':1},
        {'id':'Body','size_half_points':22,'spacing_after_twips':160}],
        'page':{'width_twips':16838,'height_twips':11906,'margin_twips':900},
        'blocks':[{'kind':'paragraph','style':'Title','text':'Original report design'},
                  {'kind':'paragraph','style':'Body','text':'Evidence-backed introduction.'},
                  {'kind':'paragraph','text':'First item','list_kind':'bullet'},
                  {'kind':'paragraph','text':'Numbered item','list_kind':'number'},
                  {'kind':'table','rows':[['Item','Budget'],['Cedar','EUR 120']]},
                  {'kind':'paragraph','text':'Next page','page_break_before':True}]}


@pytest.mark.parametrize('filename',['report.docx','report.DOCX'])
def test_creation_is_validated_staged_and_recoverably_saved(tmp_path,filename):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        args={**design(),'path':filename};receipt=plugin.create(**args,idempotency_key='new')
        assert BatchReceipt(**receipt).input_revision is None
        assert not (tmp_path/filename).exists()
        assert plugin.create(**args,idempotency_key='new')==receipt
        data=store.blob(receipt['candidate_revision'])
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            doc=ET.fromstring(package.read('word/document.xml'))
            assert len(doc.findall('.//{'+W+'}tbl'))==1
            assert len(doc.findall('.//{'+W+'}numPr'))==2
            assert doc.find('.//{'+W+'}pgSz').get('{'+W+'}orient')=='landscape'
            styles=ET.fromstring(package.read('word/styles.xml'))
            assert len(styles.findall('{'+W+'}style'))==3
            assert styles.find('.//{'+W+'}color').get('{'+W+'}val')=='17365D'
        store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='save')
        original=(tmp_path/filename).read_bytes()
        with pytest.raises(HarnessError,match='existing'):
            plugin.create(**args,idempotency_key='another')
        removal=store.undo(receipt['receipt_id'],'remove-new')
        store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='remove')
        assert not (tmp_path/filename).exists()
        restore=store.undo(removal['id'],'restore-new')
        store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/filename).read_bytes()==original
    finally:store.close()


def test_creation_rejects_ambiguous_styles_bad_geometry_and_oversized_payload():
    args=design()
    with pytest.raises(ValidationError):CreateDocument(**{**args,'styles':[]})
    with pytest.raises(ValidationError):CreateDocument(**{**args,'page':{'width_twips':2880,'height_twips':2880,'margin_twips':1000}})
    with pytest.raises(ValidationError):CreateDocument(path='a.docx',blocks=[{'kind':'table','rows':[['x'],['y','z']]}])
    with pytest.raises(ValidationError):CreateDocument(path='a.docx',blocks=[{'kind':'paragraph','text':'界'*7000}]*2)
