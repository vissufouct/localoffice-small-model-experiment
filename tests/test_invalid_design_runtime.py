"""Retained invalid design is corrected explicitly before any document staging."""
import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore,digest
from localoffice.word.creation_repair import pending
from test_native_runner import NativeProvider


@pytest.mark.asyncio
async def test_invalid_break_routes_to_compact_revision_then_finalization(tmp_path):
    objective='Create "First" and "Second" with a page break before the second paragraph.'
    store=ProjectStore(tmp_path)
    class Provider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            task=store.db.execute('SELECT id FROM tasks').fetchone()[0]
            names={t['function']['name'] for t in tools}
            if self.calls==0:
                action={'tool':'word.select_source_quotes','source_hash':digest(objective.encode()),'selections':{'q01':'paragraph','q02':'paragraph'}}
            elif self.calls==1:
                action={'tool':'word.prepare_creation','path':'report.docx','blocks':[
                    {'kind':'paragraph','text':'First'},{'kind':'paragraph','text':'Second'},{'kind':'page_break'}]}
            elif self.calls==2:
                state=pending(store,task)
                assert state['status']=='invalid_design'
                assert 'word.edit_creation_blocks' in names and 'word.finalize_creation' not in names
                assert 'invalid_design' in messages[0]['content']
                assert 'details_in_current_state' in messages[-1]['content']
                self.bad_ref=state['ref']
                action={'tool':'word.edit_creation_blocks','ref':state['ref'],'changes':[
                    {'op':'remove_block','index':2},
                    {'op':'set_paragraph','index':1,'page_break_before':True}]}
            else:
                assert self.calls==3
                state=pending(store,task)
                assert state['status']=='prepared' and state['ref']!=self.bad_ref
                assert {'word.finalize_creation','word.revise_creation','word.edit_creation_blocks'}<=names
                action={'tool':'ask_user','text':'Scripted test stops before document creation.'}
            assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Provider(None);runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        key=await runner.start(objective,artifact='report.docx',mode='background');await runner.job
        assert provider.calls==4,runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused'
        assert not any(e['type']=='tool_error' for e in runner.events)
        assert store.candidate('report.docx') is None
    finally:await runner.pause();store.close()


def test_invalid_proposal_crash_recovery_restores_revision_tools_only(tmp_path):
    from test_word_content_plan import fixture
    from localoffice.native_history import record_call,reconcile_word_call
    from localoffice.word.plugin import WordPlugin
    from localoffice.word.creation_repair import get
    store=fixture(tmp_path,'Create First and Second.')
    proposal={'path':'report.docx','blocks':[{'kind':'paragraph','text':'First'},{'kind':'page_break'}]}
    try:
        call=record_call(store,'task',{'tool':'word.prepare_creation',**proposal})
        receipt=WordPlugin(store).prepare_creation(task_id='task',native_call_id=call,**proposal)
        assert receipt['status']=='invalid_design'
        before=get(store,'task')['payload_hash']
        recovered=reconcile_word_call(store,'task')
        assert recovered['state']=='reconciled' and recovered['call_id']==call
        state=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',('word_tools:task',)).fetchone()[0])
        assert state['operations']==['edit_creation_blocks']
        assert get(store,'task')['payload_hash']==before
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert reconcile_word_call(store,'task')['state']=='no_pending_call'
    finally:store.close()
