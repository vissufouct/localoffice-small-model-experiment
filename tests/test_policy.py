import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner,Action
from localoffice.policy import task_policy
from localoffice.chats import create_chat,select_chat
from localoffice.errors import HarnessError
from pydantic import ValidationError

class Provider:
    context_limit=4096
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 100
    async def stream(self,messages,**kwargs):
        yield {'type':'token','text':json.dumps({'tool':'finish','text':'Ready'})}
        yield {'type':'finish','reason':'stop'}

@pytest.mark.asyncio
async def test_task_policy_defaults_local_and_does_not_grant_external_processing(tmp_path):
    store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    try:
        first=await runner.start('Answer locally');await runner.job
        assert task_policy(store,first)['online_research_opt_in'] is False
        second=await runner.start('Research if available',online_research=True);await runner.job
        policy=task_policy(store,second)
        assert policy['online_research_opt_in'] is True
        assert policy['effective_network']=='local_only'
        assert not any(policy[k] for k in ('external_ai_allowed','project_uploads_allowed','research_connector_available'))
        with pytest.raises(ValidationError):Action.model_validate({'tool':'web_search','text':'query'})
        with pytest.raises(ValidationError):Action.model_validate({'tool':'upload','path':'source.pdf'})
        select_chat(store,create_chat(store,'Other'))
        with pytest.raises(HarnessError,match='not found'):task_policy(store,second)
    finally:store.close()

@pytest.mark.asyncio
async def test_policy_survives_restart_and_cannot_expand_on_resume(tmp_path):
    store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    key=await runner.start('Local only');await runner.job
    with store.transaction():store.db.execute("UPDATE tasks SET status='paused' WHERE id=?",(key,))
    store.close();store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    try:
        with pytest.raises(HarnessError,match='retains'):
            await runner.start('Continue',task_id=key,online_research=True)
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused'
        await runner.start('Continue',task_id=key);await runner.job
        assert task_policy(store,key)['online_research_opt_in'] is False
    finally:store.close()
