"""Regression for a real model putting an edit into a read-only tool call."""
import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore


@pytest.mark.asyncio
@pytest.mark.parametrize('corrects', [False, True])
async def test_read_payload_requires_correction_before_completion(tmp_path, corrects):
    original='# Brief\n\nOriginal.\n'
    changed=original.replace('Original.', 'Revised.')
    (tmp_path/'brief.md').write_text(original)
    store=ProjectStore(tmp_path)
    revision=store.file_hash('brief.md')
    actions=[{'tool':'read_file','path':'brief.md','text':changed,'expected_revision':revision}]
    if corrects:
        actions.append({'tool':'stage_markdown','path':'brief.md','text':changed,'expected_revision':revision})
    actions.append({'tool':'finish','text':'Finished'})
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 400
        async def stream(self,messages,**kwargs):
            if len(actions)<(3 if corrects else 2):
                assert 'stage_markdown' in json.dumps(messages)
            yield {'type':'token','text':json.dumps(actions.pop(0))}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Revise brief.md',artifact='brief.md');await runner.job
        assert (tmp_path/'brief.md').read_text()==original
        errors=[e for e in runner.events if e['type']=='tool_error']
        assert errors and 'Read tools require empty text' in errors[0]['message']
        status=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        if corrects:
            assert status=='completed'
            assert store.read('brief.md')['text']==changed
            assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        else:
            assert status=='failed' and store.candidate('brief.md') is None
            assert not any(e['type']=='completed' for e in runner.events)
            assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()

@pytest.mark.asyncio
async def test_malformed_finish_can_be_repaired_without_repeating_staged_edit(tmp_path):
    store=ProjectStore(tmp_path)
    replies=[json.dumps({'tool':'stage_markdown','path':'new.md','text':'# Draft'}),
             'The draft is ready.',
             json.dumps({'tool':'finish','text':'Working copy ready for review'})]
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 400
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':replies.pop(0)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Create new.md',artifact='new.md');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        assert store.candidate('new.md') and not (tmp_path/'new.md').exists()
        assert len([e for e in runner.events if e['type']=='tool_error'])==1
    finally:store.close()
