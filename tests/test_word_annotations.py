import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.annotations import annotate
from localoffice.runtime import TaskRunner
from localoffice.errors import HarnessError
from test_word_worker import fixture_docx


@pytest.mark.asyncio
@pytest.mark.parametrize('broaden',[False,True])
async def test_word_object_scope_is_enforced_across_real_worker(tmp_path,broaden):
    fixture_docx(tmp_path/'a.docx');original=(tmp_path/'a.docx').read_bytes()
    store=ProjectStore(tmp_path);plugin=WordPlugin(store);revision=store.file_hash('a.docx')
    objects=plugin.list_objects(path='a.docx',revision=revision)['objects']
    annotation=annotate(store,path='a.docx',revision=revision,scope='selection',
        locator={'object_id':objects[0]['id'],'object_hash':objects[0]['hash']},comment='Change this paragraph style.')
    target=objects[1] if broaden else objects[0]
    replies=[{'tool':'word.stage_batch','path':'a.docx','revision':revision,'operations':[
        {'id':'e1','op':'set_style','target':target['id'],'expected_hash':target['hash'],'style_id':'Heading1'}]},
        {'tool':'finish','text':'Ready'}]
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 400
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':json.dumps(replies.pop(0))}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Revise annotated paragraph',artifact='a.docx',annotation_ids=[annotation]);await runner.job
        status=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        assert status==('failed' if broaden else 'completed')
        assert bool(store.candidate('a.docx')) is (not broaden)
        assert (tmp_path/'a.docx').read_bytes()==original
        if not broaden:
            candidate=store.candidate('a.docx')
            after=plugin.list_objects(path='a.docx',revision=candidate['candidate_hash'])['objects']
            assert after[1]['hash']==objects[1]['hash']
            scope=store.db.execute('SELECT head_revision FROM task_scopes WHERE task_id=?',(task,)).fetchone()[0]
            assert scope==candidate['candidate_hash']
    finally:store.close()


def test_word_annotation_requires_exact_structural_selection(tmp_path):
    fixture_docx(tmp_path/'a.docx');store=ProjectStore(tmp_path);revision=store.file_hash('a.docx')
    try:
        with pytest.raises(HarnessError,match='structural'):
            annotate(store,path='a.docx',revision=revision,scope='selection',locator={'quote':'A'},comment='Edit')
        with pytest.raises(HarnessError,match='changed'):
            annotate(store,path='a.docx',revision=revision,scope='selection',locator={'object_id':'paragraph:1','object_hash':'0'*64},comment='Edit')
        annotate(store,path='a.docx',revision=revision,scope='artifact',locator={},comment='Explicit broader redesign')
    finally:store.close()
