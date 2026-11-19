import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner

class Provider:
    context_limit=4096
    def __init__(self,actions):self.actions=iter(actions)
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 100
    async def stream(self,messages,**kwargs):
        yield {'type':'token','text':json.dumps(next(self.actions))}
        yield {'type':'finish','reason':'stop'}

@pytest.mark.asyncio
async def test_question_pauses_new_artifact_then_restarts_same_task(tmp_path):
    store=ProjectStore(tmp_path)
    runner=TaskRunner(store,Provider([{'tool':'ask_user','text':'What is the project budget?'}]))
    key=await runner.start('Create a budget note; ask for missing budget.',artifact='note.md');await runner.job
    assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused'
    assert runner.active==key and runner.events[-1]['type']=='question'
    assert not store.candidate('note.md') and not (tmp_path/'note.md').exists()
    assert not store.db.execute('SELECT 1 FROM operations').fetchone()
    store.close();store=ProjectStore(tmp_path)
    try:
        runner=TaskRunner(store,Provider([{'tool':'stage_markdown','path':'note.md','text':'Budget: EUR 120.','expected_revision':None},{'tool':'finish','text':'Ready for review.'}]))
        assert runner.active==key
        assert await runner.start('Budget: EUR 120.',task_id=key)==key
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed'
        assert store.read('note.md')['text']=='Budget: EUR 120.'
        assert not (tmp_path/'note.md').exists()
    finally:store.close()

@pytest.mark.asyncio
async def test_empty_question_cannot_silently_pause(tmp_path):
    store=ProjectStore(tmp_path)
    try:
        runner=TaskRunner(store,Provider([{'tool':'ask_user','text':''}]*2))
        key=await runner.start('Create note',artifact='note.md');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='failed'
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
    finally:store.close()
