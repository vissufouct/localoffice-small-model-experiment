import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.errors import HarnessError
from test_word_story_reads import fixture
from test_word_creation import W


def target(plugin,rev,root='header:1'):
    return plugin.list_objects(path='report.docx',revision=rev,parent=root,kind='paragraph')['objects'][0]


def apply_edit(plugin,rev,obj,op,key='story',task_id=None):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'target':obj['id'],'expected_hash':obj['hash'],**op}],idempotency_key=key,task_id=task_id)


@pytest.mark.parametrize('op',[
    {'op':'replace_text','start':0,'end':9,'text':'Annual'},
    {'op':'format_text','start':0,'end':9,'properties':{'bold':True}},
    {'op':'format_paragraph','properties':{'alignment':'center'}},
    {'op':'set_style','style_id':'Body'},
])
def test_story_paragraph_edit_changes_only_header_and_undo_is_exact(tmp_path,op):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        obj=target(plugin,rev);result=apply_edit(plugin,rev,obj,op)
        assert 'shared header/footer' in result['results'][0]['summary']
        read=plugin.read_objects(path='report.docx',revision=result['candidate_revision'],ids=[obj['id']],include_format=True)['objects'][0]
        assert read['text']==('Annual report' if op['op']=='replace_text' else 'Quarterly report')
        if op['op']=='format_paragraph':assert read['direct_format']['properties']['alignment']=='center'
        if op['op']=='set_style':assert read['direct_format']['properties']['style_id']=='Body'
        if op['op']=='format_text':assert read['direct_format']['runs'][0]['bold'] is True
        with zipfile.ZipFile(io.BytesIO(original)) as before,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as after:
            assert set(before.namelist())==set(after.namelist())
            changed=[n for n in before.namelist() if before.read(n)!=after.read(n)]
            assert changed==['word/header1.xml']
        assert apply_edit(plugin,rev,obj,op)==result
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-story');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_field_footer_text_rejection_is_atomic_with_prior_header_edit(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        header=target(plugin,rev);footer=target(plugin,rev,'footer:1')
        with pytest.raises(HarnessError,match='touches or crosses a field'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[
                {'op':'replace_text','target':header['id'],'expected_hash':header['hash'],'start':0,'end':9,'text':'Annual'},
                {'op':'replace_text','target':footer['id'],'expected_hash':footer['hash'],'start':0,'end':6,'text':'Sheet'}],idempotency_key='atomic')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
        with pytest.raises(HarnessError,match='stale_object'):
            apply_edit(plugin,rev,{**header,'hash':'0'*64},{'op':'format_paragraph','properties':{'bold':True}},key='stale')
    finally:store.close()


def test_body_annotation_cannot_authorize_shared_header_edit(tmp_path):
    from localoffice.annotations import annotate,bind_annotations
    from test_word_progress import task
    store,plugin,original,rev=fixture(tmp_path)
    try:
        body=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:1'])['objects'][0]
        task(store);a=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':body['id'],'object_hash':body['hash']},comment='Only this body paragraph')
        bind_annotations(store,'task','report.docx',[a])
        with pytest.raises(HarnessError,match='outside the annotated'):
            apply_edit(plugin,rev,target(plugin,rev),{'op':'format_paragraph','properties':{'bold':True}},task_id='task')
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_footer_formatting_preserves_fields_and_story_annotation_scope(tmp_path):
    from localoffice.annotations import annotate,bind_annotations
    from test_word_progress import task
    store,plugin,original,rev=fixture(tmp_path)
    try:
        footer=target(plugin,rev,'footer:1');task(store)
        a=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':footer['id'],'object_hash':footer['hash']},comment='Right align this footer paragraph')
        bind_annotations(store,'task','report.docx',[a])
        result=apply_edit(plugin,rev,footer,{'op':'format_paragraph','properties':{'alignment':'right'}},task_id='task')
        with zipfile.ZipFile(io.BytesIO(original)) as before,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as after:
            assert [n for n in before.namelist() if before.read(n)!=after.read(n)]==['word/footer1.xml']
            old=ET.fromstring(before.read('word/footer1.xml'));new=ET.fromstring(after.read('word/footer1.xml'))
            assert [ET.canonicalize(ET.tostring(f)) for f in old.findall('.//{'+W+'}fldSimple')]==[ET.canonicalize(ET.tostring(f)) for f in new.findall('.//{'+W+'}fldSimple')]
        updated=target(plugin,result['candidate_revision'],'footer:1')
        assert apply_edit(plugin,result['candidate_revision'],updated,{'op':'format_paragraph','properties':{'alignment':'right'}},key='noop',task_id='task')['status']=='unchanged'
    finally:store.close()
