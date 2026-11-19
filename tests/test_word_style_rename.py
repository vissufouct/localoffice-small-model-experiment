import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import RenameStyle, LoadTools
from localoffice.word.guidance import MODELS, GUIDES
from test_word_tables import saved_table
from test_word_styles import style_read
from test_word_lists import rewrite
from test_word_creation import W


def rename(plugin,rev,target,name,key='rename',task_id=None):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'rename_style','target':target['id'],'expected_hash':target['hash'],'name':name}],idempotency_key=key,task_id=task_id)


@pytest.mark.parametrize('kind',['paragraph','character','table','numbering'])
def test_rename_preserves_identity_properties_references_and_undo(tmp_path,kind):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        def add_style(parts):
            root=ET.fromstring(parts['word/styles.xml'])
            style=ET.SubElement(root,'{'+W+'}style',{'{'+W+'}styleId':'Extra','{'+W+'}type':kind})
            ET.SubElement(style,'{'+W+'}name',{'{'+W+'}val':'Old name'})
            ET.SubElement(style,'{'+W+'}aliases',{'{'+W+'}val':'Historic name'})
            parts['word/styles.xml']=ET.tostring(root)
        rewrite(tmp_path,original,add_style);original=(tmp_path/'report.docx').read_bytes();rev=store.file_hash('report.docx')
        target=style_read(plugin,rev,'Extra');result=rename(plugin,rev,target,'Estilo — revisão')
        updated=style_read(plugin,result['candidate_revision'],'Extra')
        assert updated['direct_format']['properties']['name']=='Estilo — revisão'
        assert updated['direct_format']['properties']['type']==kind
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert old.namelist()==new.namelist()
            for part in old.namelist():
                if part!='word/styles.xml':assert old.read(part)==new.read(part)
            before=ET.fromstring(old.read('word/styles.xml'));after=ET.fromstring(new.read('word/styles.xml'))
            style=next(s for s in after if s.get('{'+W+'}styleId')=='Extra')
            style.find('{'+W+'}name').set('{'+W+'}val','Old name')
            assert ET.canonicalize(ET.tostring(before))==ET.canonicalize(ET.tostring(after))
        assert rename(plugin,rev,target,'Estilo — revisão')==result
        assert rename(plugin,result['candidate_revision'],updated,'Estilo — revisão',key='noop')['status']=='unchanged'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-rename');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_rename_rejects_collisions_stale_wrong_targets_and_selected_scope(tmp_path):
    from localoffice.annotations import annotate,bind_annotations
    from test_word_progress import task
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        normal=style_read(plugin,rev,'Normal')
        with pytest.raises(HarnessError,match='name already exists'):
            rename(plugin,rev,target,normal['direct_format']['properties']['name'].swapcase())
        with pytest.raises(HarnessError,match='stale_object'):
            rename(plugin,rev,{**target,'hash':'0'*64},'New',key='stale')
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError,match='style target'):rename(plugin,rev,paragraph,'New',key='wrong')
        task(store);annotation=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':paragraph['id'],'object_hash':paragraph['hash']},comment='Only this paragraph')
        bind_annotations(store,'task','report.docx',[annotation])
        with pytest.raises(HarnessError,match='outside the annotated'):rename(plugin,rev,target,'New',key='scope',task_id='task')
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_rename_contract_and_guidance():
    assert MODELS['rename_style'] is RenameStyle and 'style ID' in GUIDES['rename_style']
    LoadTools(operations=['rename_style'])
    for name in ['', '   ', '\x00name', '\ud800', 'a'*121]:
        with pytest.raises(ValidationError):RenameStyle(target='style:Body',expected_hash='a'*64,name=name)
    with pytest.raises(ValidationError):RenameStyle(target='style:Body',expected_hash='a'*64,name='New',style_id='New')
