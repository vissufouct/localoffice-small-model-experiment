"""Proposal navigation survives tool unloading without blocking source reads."""
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical
from localoffice.word.creation_repair import capture
from localoffice.word.guidance import build_state,fingerprint
from test_word_content_plan import fixture
from test_native_runner import NativeProvider


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',[None,'inspect','revise_creation'])
async def test_fresh_proposal_has_navigation_even_after_inspect_reload(tmp_path,operation):
    store=fixture(tmp_path,'Create a short report.')
    runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
    try:
        proposal=capture(store,'task',{'path':'report.docx','blocks':[{'kind':'paragraph','text':'Draft'}]},allow_valid=True)
        state=build_state([operation]) if operation else {'operations':[],'source_hash':fingerprint([]),'guidance':{}}
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(state)))
        messages=await runner.assemble('task')
        assert 'Retained proposal only: the target has no document revision.' in messages[0]['content']
        assert proposal['ref'] in messages[0]['content']
        assert 'inspect reads existing source files, not this proposal.' in messages[0]['content']
        loader=next(t['function'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
        assert {'inspect','revise_creation','edit_creation_blocks','finalize_creation'}<=set(loader['parameters']['properties']['operations']['items']['enum'])
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        store.db.execute("UPDATE tasks SET objective='Changed objective' WHERE id='task'")
        messages=await runner.assemble('task')
        assert 'Retained proposal only:' not in messages[0]['content']
    finally:await runner.pause();store.close()
