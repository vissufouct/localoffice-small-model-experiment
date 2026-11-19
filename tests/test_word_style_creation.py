import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import CreateParagraphStyle,StageBatch
from test_word_tables import saved_table
from test_word_styles import style_read
from test_word_lists import rewrite
from test_word_creation import W


def create(plugin,rev,target,*,style_id='Callout',name='Report callout',properties=None,key='create-style',task_id=None):
    op={'id':'create','op':'create_paragraph_style','target':target['id'],'expected_hash':target['hash'],'style_id':style_id,'name':name}
    if properties is not None:op['properties']=properties
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key=key,task_id=task_id)


def test_style_creation_changes_only_styles_preserves_base_and_exact_undo(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev);properties={'size_points':14,'bold':True,'color':'17365D','spacing_before_twips':120}
        result=create(plugin,rev,target,properties=properties)
        assert result['results'][0]['inserted_ids']==['style:Callout']
        created=style_read(plugin,result['candidate_revision'],'Callout')['direct_format']['properties']
        assert created['based_on']=='Body' and created['name']=='Report callout'
        assert created['size_half_points']=='28' and created['bold'] is True and created['spacing_before_twips']=='120'
        assert not created['default'] and 'italic' not in created and 'spacing_after_twips' not in created
        assert style_read(plugin,result['candidate_revision'])['hash']==target['hash']
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/styles.xml'));b=ET.fromstring(new.read('word/styles.xml'))
            b.remove(next(s for s in b if s.get('{'+W+'}styleId')=='Callout'))
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert create(plugin,rev,target,properties=properties)==result
        assert create(plugin,result['candidate_revision'],target,properties=properties,key='same')['status']=='unchanged'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-style-create');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_inherited_style_can_be_assigned_separately_and_cannot_be_overwritten(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev);result=create(plugin,rev,target)
        rev=result['candidate_revision'];data=style_read(plugin,rev,'Callout')['direct_format']['properties']
        assert data=={'style_id':'Callout','type':'paragraph','default':False,'name':'Report callout','based_on':'Body'}
        with pytest.raises(HarnessError,match='cannot overwrite'):create(plugin,rev,target,properties={'bold':True},key='collision')
        with pytest.raises(HarnessError,match='name already exists'):create(plugin,rev,target,style_id='Other',name='report CALLOUT',key='duplicate')
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        applied=plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'assign','op':'set_style','target':paragraph['id'],'expected_hash':paragraph['hash'],'style_id':'Callout'}],idempotency_key='assign')
        read=plugin.read_objects(path='report.docx',revision=applied['candidate_revision'],ids=[paragraph['id']],include_format=True)['objects'][0]
        assert read['text']==paragraph['text'] and read['direct_format']['properties']['style_id']=='Callout'
    finally:store.close()


@pytest.mark.parametrize('parent',['Callout','Missing'])
def test_style_creation_rejects_inheritance_cycle_or_missing_ancestor(tmp_path,parent):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        def invalid(parts):
            root=ET.fromstring(parts['word/styles.xml']);style=next(s for s in root if s.get('{'+W+'}styleId')=='Body')
            ET.SubElement(style,'{'+W+'}basedOn',{'{'+W+'}val':parent});parts['word/styles.xml']=ET.tostring(root)
        rewrite(tmp_path,original,invalid);rev=store.file_hash('report.docx')
        with pytest.raises(HarnessError,match='cycle|Unresolved'):create(plugin,rev,style_read(plugin,rev))
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_style_creation_rejects_stale_target_invalid_names_and_selected_scope(tmp_path):
    from localoffice.annotations import annotate,bind_annotations
    from test_word_progress import task
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        with pytest.raises(HarnessError,match='stale_object'):create(plugin,rev,{**target,'hash':'0'*64})
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError,match='base style target'):create(plugin,rev,paragraph)
        for name in ['','  ','Invalid\x01']:
            with pytest.raises(ValidationError):CreateParagraphStyle(id='new',target=target['id'],expected_hash=target['hash'],style_id='Callout',name=name)
        task(store);annotation=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':paragraph['id'],'object_hash':paragraph['hash']},comment='Only paragraph')
        bind_annotations(store,'task','report.docx',[annotation])
        with pytest.raises(HarnessError,match='whole-artifact'):create(plugin,rev,target,task_id='task')
        assert store.candidate('report.docx') is None
    finally:store.close()
