"""Prepared proposals expose correction choices without selecting or executing edits."""
import json
import pytest
from localoffice.word.creation_routing import next_operation,next_operations
from localoffice.word.guidance import build_state
from localoffice.word.creation_repair import capture
from localoffice.native_history import _restore_proposal_routing
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical
from test_word_content_plan import fixture
from test_native_runner import NativeProvider

CHOICES=['edit_creation_blocks','revise_creation','finalize_creation']


def test_prepared_choices_preserve_single_operation_compatibility():
    assert next_operation({'status':'prepared'})=='finalize_creation'
    assert next_operations({'status':'prepared'})==CHOICES
    assert next_operations({'status':'stale'})==[next_operation({'status':'stale'})]
    for errors,wanted in [({'items':[{'code':'page_break_position'}],'omitted_count':0},'edit_creation_blocks'),
                          ({'items':[],'omitted_count':0},'revise_creation')]:
        state={'status':'invalid_design','validation_errors':errors}
        assert next_operations(state)==[wanted]


def test_recovery_routing_restores_choices_only(tmp_path):
    store=fixture(tmp_path)
    try:
        with store.transaction():_restore_proposal_routing(store,'task',{'status':'prepared'})
        state=json.loads(store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0])
        assert state['operations']==CHOICES
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('selection,stale,expected',[
    (CHOICES,False,CHOICES),(CHOICES,True,['prepare_creation']),
    (['revise_creation'],False,['revise_creation']),(['plan_content'],False,['plan_content']),
])
async def test_explicit_selection_and_stale_prepared_route(tmp_path,selection,stale,expected):
    store=fixture(tmp_path)
    runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
    try:
        capture(store,'task',{'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}]},allow_valid=True)
        with store.transaction():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(selection))))
            if stale:store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        await runner.assemble('task')
        assert runner.word_tool_state['operations']==expected
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()
