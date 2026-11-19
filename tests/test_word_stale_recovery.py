import json
import pytest
from localoffice.runtime import TaskRunner
from test_word_tables import saved_table

@pytest.mark.asyncio
async def test_stale_object_returns_bounded_target_read_without_auto_mutation(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 500
        async def stream(self,messages,**kwargs):
            self.step+=1
            if self.step==1:
                action={'tool':'word.stage_batch','path':'report.docx','revision':rev,'operations':[{'id':'bad','op':'format_paragraph','target':target['id'],'expected_hash':'0'*64,'properties':{'bold':True}}]}
            elif self.step==2:
                error=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1])
                assert error['code']=='stale_object'
                assert 'image asset/source hashes' in error['message']
                assert store.candidate('report.docx') is None
                assert error['recovery']=={'tool':'word.read_objects','path':'report.docx','revision':rev,'ids':['paragraph:2'],'include_format':True}
                action=error['recovery']
            else:action={'tool':'finish','text':'Read refreshed; no edit applied.'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Read and verify the target only.',artifact='report.docx');await runner.job
        # The provider attempted a mutation even though this task was read-only.
        # Refreshing evidence must not erase that failed attempt or mark it done.
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='failed'
        assert any(e.get('code')=='unresolved_tool_error' for e in runner.events)
        assert any(e['type']=='error' and e['code']=='retry_limit' for e in runner.events)
        assert not any(e['type']=='completed' for e in runner.events)
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()
