"""Current requirement facts survive native history compaction without fake calls."""
import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.native_history import record_call,record_result
from localoffice.native_tools import validate_history
from localoffice.storage import canonical,digest
from localoffice.word.requirements import plan
from localoffice.word.requirement_plan_context import snapshot,STATE_KEY
from localoffice.word.guidance import build_state
from test_word_content_plan import fixture
from test_native_runner import NativeProvider


@pytest.mark.asyncio
async def test_complete_plan_visible_and_native_pair_compacted_without_storage_mutation(tmp_path):
    objective='Use one-inch margins.'
    store=fixture(tmp_path,objective)
    runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
    try:
        args={'source_hash':digest(objective.encode()),'requirements':[
            {'kind':'paragraph_words','source_range':{'start':'s01','end':'s01'},
             'paragraph_index':index,'min':index,'max':index+20} for index in range(8)]}
        call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
        receipt=plan(store,'task',**args,native_call_id=call)
        record_result(store,'task',call,receipt)
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(['prepare_creation']))))
        original=[tuple(row) for row in store.db.execute('SELECT function,result_message FROM native_calls')]
        original_messages=[tuple(row) for row in store.db.execute('SELECT id,role,content FROM messages ORDER BY id')]
        expected=snapshot(store,'task')
        assert len(expected['requirements'])==8
        messages=await runner.assemble('task')
        validate_history(messages)
        assert canonical(expected) in messages[0]['content']
        assert STATE_KEY in messages[0]['content']
        assert not any(c['id']==call for m in messages for c in m.get('tool_calls',[]))
        assert not any(m.get('tool_call_id')==call for m in messages)
        assert any(m['role']=='assistant' and call in m['content'] for m in messages)
        assert [tuple(row) for row in store.db.execute('SELECT function,result_message FROM native_calls')]==original
        assert [tuple(row) for row in store.db.execute('SELECT id,role,content FROM messages ORDER BY id')]==original_messages
    finally:
        await runner.pause();store.close()
