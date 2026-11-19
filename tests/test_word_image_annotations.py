import json
import pytest
from localoffice.annotations import annotate,bind_annotations
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from test_word_image_format import placed,read
from test_word_image_replace import replacement
from test_word_progress import task

@pytest.mark.parametrize('operation',['format_image','replace_image'])
def test_image_annotation_limits_edits_survives_restart_and_advances_revision(tmp_path,operation):
    store,plugin,original,rev=placed(tmp_path);task(store)
    target=read(plugin,rev)
    annotation=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':target['id'],'object_hash':target['hash']},comment='Update only this picture.')
    bind_annotations(store,'task','report.docx',[annotation])
    asset=replacement(tmp_path,store)
    store.close();store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError,match='outside the annotated'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'wrong','op':'format_paragraph','target':paragraph['id'],'expected_hash':paragraph['hash'],'properties':{'bold':True}}],idempotency_key='wrong',task_id='task')
        with pytest.raises(HarnessError,match='whole-artifact'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'insert','op':'insert_image','target':target['id'],'expected_hash':target['hash'],'asset_id':asset,'width_inches':2,'alt_text':'New'}],idempotency_key='insert',task_id='task')
        op={'id':'picture','op':operation,'target':target['id'],'expected_hash':target['hash']}
        op.update({'properties':{'alt_text':'Updated picture'}} if operation=='format_image' else {'asset_id':asset})
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='picture',task_id='task')
        scope=store.db.execute("SELECT * FROM task_scopes WHERE task_id='task'").fetchone()
        assert scope['head_revision']==result['candidate_revision']
        assert json.loads(scope['ranges'])=={'format':'docx','objects':[target['id']]}
        current=read(plugin,result['candidate_revision'])
        second=plugin.stage_batch(path='report.docx',revision=result['candidate_revision'],operations=[{'id':'resize','op':'format_image','target':current['id'],'expected_hash':current['hash'],'properties':{'width_inches':3}}],idempotency_key='resize',task_id='task')
        assert read(plugin,second['candidate_revision'])['direct_format']['properties']['width_inches']==3
        assert (tmp_path/'report.docx').read_bytes()==original
        with pytest.raises(HarnessError,match='older revision'):
            bind_annotations(store,'task','report.docx',[annotation])
    finally:store.close()


def test_image_annotation_requires_current_hash_and_rejects_unimplemented_object_scope(tmp_path):
    store,plugin,original,rev=placed(tmp_path)
    try:
        with pytest.raises(HarnessError,match='changed'):
            annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':'image:1','object_hash':'0'*64},comment='Change picture')
        section=plugin.read_objects(path='report.docx',revision=rev,ids=['section:1'])['objects'][0]
        with pytest.raises(HarnessError,match='other object selection'):
            annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':section['id'],'object_hash':section['hash']},comment='Section')
    finally:store.close()
