import json
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.plugins import first_party_registry
from localoffice.runtime import WORD_ACTION,TaskRunner
from localoffice.word.contracts import ReplaceTextCall
from localoffice.word.guidance import project_schema,prompt_schemas
from test_word_tables import saved_table


def arguments(plugin,rev):
    target=plugin.find_text(path='report.docx',revision=rev,query='Evidence-backed')['matches'][0]
    return {'path':'report.docx','revision':rev,'target':target['id'],'expected_hash':target['hash'],'start':target['start'],'end':target['end'],'text':'Source-backed'}


def test_single_call_schema_is_lazy_compact_and_preserves_strict_semantics():
    full=WORD_ACTION.json_schema()
    assert 'ReplaceTextCall' not in project_schema(full,[])['$defs']
    assert 'ReplaceTextCall' in project_schema(full,['replace_text'])['$defs']
    assert set(prompt_schemas(['replace_text']))=={'word.replace_text'}
    assert 'word.stage_batch' in prompt_schemas(['replace_text','set_style'])
    for tool in ['word.replace_text','replace_text']:
        call=WORD_ACTION.validate_python({'tool':tool,'path':'x.docx','revision':'a'*64,'target':'paragraph:2','expected_hash':'b'*64,'start':0,'end':15,'text':'Source-backed'})
        assert call.as_batch().operations[0].op=='replace_text'
        with pytest.raises(ValidationError):WORD_ACTION.validate_python({**call.model_dump(),'replacement':'Source-backed'})
        with pytest.raises(ValidationError):WORD_ACTION.validate_python({**call.model_dump(),'properties':{'bold':True}})
        with pytest.raises(ValidationError):WORD_ACTION.validate_python({k:v for k,v in call.model_dump().items() if k!='expected_hash'})


@pytest.mark.asyncio
async def test_registered_aliases_share_normalized_batch_ledger_replay_and_plugin_disable(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);registry=first_party_registry(store)
    try:
        args=arguments(plugin,rev)
        result=await registry.call('replace_text',**args,idempotency_key='shared')
        assert await registry.call('word.replace_text',**args,idempotency_key='shared')==result
        batch=ReplaceTextCall(**args).as_batch()
        assert plugin.stage_batch(**batch.model_dump(exclude={'tool'}),idempotency_key='shared')==result
        changed=plugin.read_objects(path='report.docx',revision=result['candidate_revision'],ids=[args['target']])['objects'][0]
        assert changed['text']=='Source-backed introduction.'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-direct');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
        registry.set_enabled('word',False)
        for name in ['replace_text','word.replace_text']:
            with pytest.raises(HarnessError,match='disabled'):registry.call(name,**args,idempotency_key='disabled')
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_scope',[False,True])
async def test_direct_call_uses_task_scope_and_live_render_boundary(tmp_path,wrong_scope):
    from localoffice.annotations import annotate
    store,plugin,original,rev=saved_table(tmp_path);args=arguments(plugin,rev)
    target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:1' if wrong_scope else 'paragraph:2'])['objects'][0]
    annotation=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':target['id'],'object_hash':target['hash']},comment='Change this paragraph only')
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 500
        async def stream(self,messages,**kwargs):
            self.step+=1
            action={'tool':'word.replace_text',**args} if self.step==1 else {'tool':'finish','text':'Ready'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider());rendered=[]
    async def boundary(task,path,revision):rendered.append((task,path,revision))
    runner.render_word_boundary=boundary
    try:
        key=await runner.start('Replace the quoted text',artifact='report.docx',annotation_ids=[annotation],mode='live');await runner.job
        candidate=store.candidate('report.docx')
        if wrong_scope:
            assert candidate is None and rendered==[]
            assert any(e.get('code')=='scope_violation' for e in runner.events)
        else:
            assert candidate and rendered==[(key,'report.docx',candidate['candidate_hash'])]
            assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed'
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_malformed_direct_call_gets_schema_loading_recovery_without_inferred_edits(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);args=arguments(plugin,rev)
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 500
        async def stream(self,messages,**kwargs):
            self.step+=1
            if self.step==1:action={'tool':'replace_text',**{k:v for k,v in args.items() if k!='text'},'replacement':'Source-backed'}
            elif self.step==2:
                error=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1])
                assert store.candidate('report.docx') is None
                assert error['recovery']=={'tool':'word.load_tools','operations':['replace_text']}
                action=error['recovery']
            elif self.step==3:
                assert 'ReplaceTextCall' in kwargs['schema']['$defs']
                action={'tool':'word.replace_text',**args}
            else:action={'tool':'finish','text':'Ready'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        key=await runner.start('Replace quoted text',artifact='report.docx');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed'
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(key,)).fetchone()[0]==1
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()
