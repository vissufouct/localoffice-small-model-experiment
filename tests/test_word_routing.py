import pytest
from localoffice.word.routing import initial_operations


@pytest.mark.parametrize('objective',[
    'Remove the entire paragraph containing the obsolete disclaimer.',
    'In notes.docx delete this paragraph: Draft.',
    'Please REMOVE a whole paragraph and then update the table.',
    'Delete paragraph 3 after reviewing it.',
])
def test_explicit_paragraph_removal_offers_capability(objective):
    assert initial_operations(objective)==['delete_paragraph']


@pytest.mark.parametrize('objective',[
    'Do not delete the paragraph.', "Don't remove the paragraph.",
    'Never remove this paragraph.', 'Explain whether to delete a paragraph.',
    'For example remove a paragraph.', 'Remove the paragraph text but retain its spacing.',
    "Remove the paragraph's contents.", 'Remove the paragraph border.',
    'Replace a word in the paragraph.', 'Delete a row.', 'Clear this paragraph.',
    'Create a report.', 'Should I remove a paragraph?', 'Remove paragraph numbering.',
    'Remove the paragraph alignment.', 'Explain how to delete a paragraph.',
])
def test_uncertain_or_other_intents_abstain(objective):
    assert initial_operations(objective)==[]


@pytest.mark.asyncio
async def test_native_routing_exposes_hint_but_does_not_execute(tmp_path):
    from localoffice.runtime import TaskRunner
    from test_word_tables import saved_table
    class Provider:
        native_tool_protocol=True
        context_limit=4096
        def status(self):return {'loaded':True,'constrained_decoding':False}
        async def count_tokens(self,messages,*,tools):return 500
        async def stream(self,messages,*,tools,**kwargs):
            import json
            encoded=json.dumps(tools)
            assert 'delete_paragraph' in encoded
            batch=next(t['function'] for t in tools if t['function']['name']=='word.stage_batch')
            assert 'DeleteParagraph' in batch['parameters']['$defs']
            assert 'ReplaceText' not in batch['parameters']['$defs']
            assert any(t['function']['name']=='word.load_tools' for t in tools)
            yield {'type':'token','text':'<tool_call>{"name":"ask_user","arguments":{"text":"Which paragraph?"}}</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    store,plugin,original,rev=saved_table(tmp_path);runner=TaskRunner(store,Provider())
    try:
        key=await runner.start('Remove the entire paragraph I will select next.',artifact='report.docx');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused',runner.events
        assert not store.candidate('report.docx')
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(key,)).fetchone()[0]==0
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()
