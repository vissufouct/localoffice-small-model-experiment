"""Typed style proposal edits: recovery, capability routing and native identity."""
import copy
import json
import pytest

from localoffice.native_history import record_call, reconcile_word_call
from localoffice.native_tools import action_tools, creation_state_tools
from localoffice.memory import forget_project_memory
from localoffice.runtime import WORD_ACTION, CREATION_ONLY_OPERATIONS
from localoffice.word.guidance import project_schema
from localoffice.word.plugin import WordPlugin
from localoffice.word.creation_routing import next_operations, style_correction_useful
from test_word_content_plan import fixture
from test_native_runner import NativeProvider
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical
from localoffice.word.guidance import build_state
from localoffice.recovery import load as recovery
from localoffice.chats import current_chat


def test_style_receipts_recover_only_executed_call_and_forget(tmp_path):
    store=fixture(tmp_path,'Create Body.')
    plugin=WordPlugin(store)
    try:
        proposal=plugin.prepare_creation(task_id='task',path='report.docx',
            blocks=[{'kind':'paragraph','text':'Body'}])
        args={'ref':proposal['ref'],'changes':[{'op':'declare','id':'Normal','font':'Arial','size_points':12}]}
        call=record_call(store,'task',{'tool':'word.edit_creation_styles',**args})
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        receipt=plugin.edit_creation_styles(task_id='task',native_call_id=call,**args)
        assert receipt['status']=='prepared'
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        record_call(store,'task',{'tool':'word.edit_creation_styles',**args})
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        assert store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'word_creation_style_result:%'").fetchone()
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        assert not store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'word_creation_style_result:%'").fetchone()
    finally:store.close()


@pytest.mark.parametrize('status',['prepared','missing_styles','invalid_design'])
def test_native_style_reference_bound_without_guessing_ids(status):
    tools=action_tools(project_schema(WORD_ACTION.json_schema(),['edit_creation_styles']))
    original=copy.deepcopy(tools)
    projected=creation_state_tools(tools,{'status':status,'ref':'cr_'+'a'*32})
    target=next(t for t in projected if t['function']['name']=='word.edit_creation_styles')
    assert target['function']['parameters']['properties']['ref']=={'const':'cr_'+'a'*32}
    expected=copy.deepcopy(original)
    next(t for t in expected if t['function']['name']=='word.edit_creation_styles')['function']['parameters']['properties']['ref']={'const':'cr_'+'a'*32}
    assert projected==expected and tools==original
    assert 'edit_creation_styles' in CREATION_ONLY_OPERATIONS


def test_font_mismatch_exposes_style_capability_without_deciding_values():
    state={'status':'prepared','selected_requirements':{'status':'mismatch','items':[
        {'kind':'paragraph_font_size','status':'mismatch','actual':11,'expected':12}]}}
    before=copy.deepcopy(state)
    assert style_correction_useful(state)
    assert next_operations(state)==['edit_creation_blocks','edit_creation_styles','finalize_creation']
    assert state==before
    state['selected_requirements']['items'][0]['kind']='page_geometry'
    assert not style_correction_useful(state)
    assert next_operations(state)==['edit_creation_blocks','revise_creation','finalize_creation']


@pytest.mark.asyncio
@pytest.mark.parametrize('prior_mutation',[False,True])
async def test_atomic_style_error_can_recover_without_erasing_prior_mutation(tmp_path,prior_mutation):
    store=fixture(tmp_path,'Create Body.')
    proposal=WordPlugin(store).prepare_creation(task_id='task',path='report.docx',
        blocks=[{'kind':'paragraph','text':'Body'}])
    class Scripted(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            changes=([{'op':'update','id':'Normal','size_points':12}] if self.calls==0
                     else [{'op':'declare','id':'Normal','font':'Arial','size_points':12}])
            action=({'name':'word.edit_creation_styles','arguments':{'ref':proposal['ref'],'changes':changes}}
                    if self.calls<2 else {'name':'ask_user','arguments':{'text':'Scripted stop.'}})
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         ('word_tools:task',canonical(build_state(['edit_creation_styles']))))
    if prior_mutation:
        store.message('tool',canonical({'code':'older_mutation','message':'Unresolved'}),'task',
                      recovery_state={'unresolved':True,'mutation':True})
    runner=TaskRunner(store,Scripted(None),source_planning=True,creation_review=True)
    try:
        await runner.run('task','background')
        assert runner.provider.calls==3,runner.events
        assert len([e for e in runner.events if e['type']=='tool_error'])==1,runner.events
        assert not [e for e in runner.events if e['type']=='error'],runner.events
        state=recovery(store,'task',current_chat(store))
        assert state['mutation'] is prior_mutation
        assert state['unresolved'] is prior_mutation
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()
