import pytest
from pydantic import ValidationError
from localoffice.runtime import WORD_ACTION
from localoffice.tool_transport import json_payload


@pytest.mark.parametrize('fence',['```json\n','```\n','```json\r\n'])
def test_exact_outer_fence_preserves_payload_and_schema_checks(fence):
    raw=fence+'{"tool":"word.load_tools","operations":["set_list"]}\n```'
    payload,changed=json_payload(raw)
    assert changed and WORD_ACTION.validate_json(payload).operations==['set_list']
    bad=fence+'{"tool":"word.load_tools","operations":["execute_script"]}\n```'
    with pytest.raises(ValidationError):WORD_ACTION.validate_json(json_payload(bad)[0])


@pytest.mark.parametrize('raw',[
    'Here is JSON: {"tool":"finish","text":"done"}',
    '```python\n{"tool":"finish","text":"done"}\n```',
    '```json\n{"tool":"finish","text":"done"}\n```\nExtra prose',
    '```json\n{"tool":"finish","text":"done"} garbage\n```',
    '```json\n{"tool":"finish","text":"done"}\n```\n```json\n{}\n```',
])
def test_no_prose_extraction_or_json_repair(raw):
    with pytest.raises(ValidationError):WORD_ACTION.validate_json(json_payload(raw)[0])


@pytest.mark.parametrize('payload',[
    '{"tool":"finish","tool":"ask_user","text":"done"}',
    '{"operations":[{"target":"cell:1","target":"cell:2"}]}',
    r'{"target":"cell:1","targ\u0065t":"cell:2"}',
    '{"properties":{"fill":"FFFFFF","fill":"FFFFFF"}}',
])
def test_duplicate_json_keys_rejected_even_when_nested_escaped_or_identical(payload):
    from localoffice.tool_transport import reject_duplicate_keys
    from localoffice.errors import HarnessError
    with pytest.raises(HarnessError) as error:reject_duplicate_keys(payload)
    assert error.value.code=='duplicate_json_key'


def test_distinct_operation_objects_can_repeat_property_names():
    from localoffice.tool_transport import reject_duplicate_keys
    reject_duplicate_keys('{"operations":[{"target":"cell:1"},{"target":"cell:2"}]}')


@pytest.mark.asyncio
async def test_runtime_duplicate_cell_targets_never_stage_a_partial_batch(tmp_path):
    import json
    from localoffice.runtime import TaskRunner
    from test_word_tables import saved_table,get
    store,plugin,original,rev=saved_table(tmp_path)
    first=get(plugin,rev,'cell:1');second=get(plugin,rev,'cell:2')
    one=json.dumps({'op':'format_cell','target':first['id'],'expected_hash':first['hash'],'properties':{'fill':'DBEAFE'}})
    repeated=one[:-1]+','+json.dumps({'target':second['id'],'expected_hash':second['hash'],'properties':{'fill':'DBEAFE'}})[1:]
    raw='{"tool":"word.stage_batch","path":"report.docx","revision":'+json.dumps(rev)+',"operations":['+repeated+']}'
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 400
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':raw}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Format both header cells',artifact='report.docx');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='failed'
        assert store.candidate('report.docx') is None
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==0
        assert any(e.get('code')=='duplicate_json_key' for e in runner.events)
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()
