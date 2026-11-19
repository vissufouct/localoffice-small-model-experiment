import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import StageBatch,LoadTools
from localoffice.word.guidance import project_schema
from localoffice.runtime import WORD_ACTION
from test_word_tables import saved_table,get
from test_word_creation import W
from test_word_table_annotations import bind


def op(plugin,rev,target,properties):
    obj=get(plugin,rev,target)
    return {'op':'format_row','target':target,'expected_hash':obj['hash'],'properties':properties}


def read(plugin,rev,target):
    return plugin.read_objects(path='report.docx',revision=rev,ids=[target],include_format=True)['objects'][0]['direct_format']['properties']


def test_row_layout_preserves_cells_and_other_parts_with_scope_replay_undo(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        bind(store,plugin,rev,['row:2'])
        change=op(plugin,rev,'row:2',{'height':{'twips':720,'rule':'at_least'},'allow_split':False})
        args=dict(path='report.docx',revision=rev,operations=[change],idempotency_key='row',task_id='table-task')
        result=plugin.stage_batch(**args);assert plugin.stage_batch(**args)==result;head=result['candidate_revision']
        props=read(plugin,head,'row:2');assert props['height']=={'twips':720,'rule':'at_least'} and props['allow_split'] is False
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(head))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            row=b.findall('.//{'+W+'}tr')[1];props_xml=row.find('{'+W+'}trPr');row.remove(props_xml)
            assert ET.tostring(a)==ET.tostring(b)
        same=plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,'row:2',change['properties'])],idempotency_key='same',task_id='table-task')
        assert same['status']=='unchanged'
        with pytest.raises(HarnessError,match='outside'):
            plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,'row:1',{'allow_split':False})],idempotency_key='wrong',task_id='table-task')
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=head,action='apply')
        undo=store.undo(result['receipt_id'],'undo-row');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_header_prefix_changes_are_ordered_atomic_and_height_is_sparse(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev,'row:2',{'repeat_header':True})],idempotency_key='repeat')
        head=result['candidate_revision'];assert read(plugin,head,'row:2')['repeat_header'] is True
        with pytest.raises(HarnessError,match='bottom-up'):
            plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,'row:1',{'height':{'twips':400,'rule':'exact'},'repeat_header':False})],idempotency_key='bad')
        assert store.candidate('report.docx')['candidate_hash']==head
        result=plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,'row:2',{'repeat_header':False}),op(plugin,head,'row:1',{'repeat_header':False})],idempotency_key='disable')
        head=result['candidate_revision']
        assert read(plugin,head,'row:1')['repeat_header'] is False
        with pytest.raises(HarnessError,match='top-down'):
            plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,'row:2',{'repeat_header':True})],idempotency_key='gap')
        result=plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,'row:1',{'height':{'twips':600,'rule':'exact'},'allow_split':True,'repeat_header':True})],idempotency_key='enable')
        props=read(plugin,result['candidate_revision'],'row:1')
        assert props=={'height':{'twips':600,'rule':'exact'},'allow_split':True,'repeat_header':True}
    finally:store.close()


@pytest.mark.parametrize('properties',[{}, {'height':{'twips':0}}, {'height':{'twips':500,'rule':'minimum'}}, {'allow_split':'yes'}])
def test_row_schema_bounds_and_lazy_registration(tmp_path,properties):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        with pytest.raises(ValidationError):StageBatch(path='report.docx',revision=rev,operations=[op(plugin,rev,'row:1',properties)])
        assert LoadTools(operations=['format_row']).operations==['format_row']
        assert 'FormatRow' not in project_schema(WORD_ACTION.json_schema(),[])['$defs']
        assert 'FormatRow' in project_schema(WORD_ACTION.json_schema(),['format_row'])['$defs']
    finally:store.close()


def test_row_insertion_keeps_formatted_anchor_properties_and_requested_header(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev,'row:1',{'height':{'twips':600},'allow_split':False,'repeat_header':False})],idempotency_key='layout')
        head=result['candidate_revision'];anchor=get(plugin,head,'row:1')
        result=plugin.stage_batch(path='report.docx',revision=head,operations=[{'op':'insert_rows','target':'row:1','expected_hash':anchor['hash'],'position':'before','header_row':True,'rows':[['New header','New value']]}],idempotency_key='insert-header')
        props=read(plugin,result['candidate_revision'],'row:1')
        assert props['repeat_header'] is True and props['allow_split'] is False
        assert props['height']=={'twips':600,'rule':'at_least'}
        assert get(plugin,result['candidate_revision'],'row:2')['hash']==anchor['hash']
    finally:store.close()
