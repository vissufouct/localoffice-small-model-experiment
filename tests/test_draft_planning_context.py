import copy
import json

import pytest

from localoffice.chats import current_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.storage import canonical
from localoffice.word import draft_fragments as draft
from localoffice.word.draft_context import build, planning_tool
from localoffice.word.draft_planning_context import planning_context
from localoffice.word.objective_evidence import catalogue
from test_draft_fragments import ctx, native_plan, TEXT


class Provider:
    context_limit = 4096

    def __init__(self, count=100):
        self.count = count
        self.requests = []

    async def count_tokens(self, messages, *, tools):
        self.requests.append(copy.deepcopy((messages, tools)))
        return self.count


def tools_for(store):
    return [planning_tool(catalogue(store, 'task')),
            {'type': 'function', 'function': {'name': 'word.load_tools', 'parameters': {
                'type': 'object', 'additionalProperties': False,
                'properties': {'operations': {'type': 'array', 'items': {'enum': ['prepare_creation', 'inspect']},
                                              'minItems': 1, 'maxItems': 1}}, 'required': ['operations']}}},
            build('Draft', 'df_' + 'a' * 32)[1][1]]


async def context(store, provider=None, decisions=(), tools=None):
    return await planning_context(store, provider or Provider(), 'task', current_chat(store),
                                  decisions, tools if tools is not None else tools_for(store))


def exchange(store, action, response):
    call = record_call(store, 'task', action)
    record_result(store, 'task', call, response)
    return call


@pytest.mark.asyncio
async def test_exact_complete_catalogue_tools_decisions_and_read_only(ctx):
    ctx.message('user', TEXT, 'task')
    decisions = [{'decision': 'Keep accents 😀.', 'scope': 'all chat'}]
    tools = tools_for(ctx)
    inputs = copy.deepcopy((decisions, tools))
    before = list(ctx.db.iterdump())
    provider = Provider()
    messages, actual_tools, info = await context(ctx, provider, decisions, tools)
    evidence = json.loads(messages[1]['content'])
    assert evidence == catalogue(ctx, 'task')
    assert ''.join(s['text'] for s in evidence['segments'].values()) == TEXT
    assert len(messages) == 2
    assert 'word.load_tools to return to document work' in messages[0]['content']
    assert canonical(decisions) in messages[0]['content']
    assert actual_tools == tools and actual_tools is not tools
    assert (decisions, tools) == inputs and list(ctx.db.iterdump()) == before
    assert provider.requests == [(messages, actual_tools)]
    assert info['output_reserve'] == 768 and info['safety_margin'] == 128
    assert info['omitted_messages'] == 0 and info['mode'] == 'isolated_draft_planning'


@pytest.mark.asyncio
async def test_all_annotations_and_edit_scope_preserved(ctx):
    with ctx.transaction():
        for number in range(20):
            key = f'a{number:02}'
            ctx.db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?)',
                (key, 'report.docx', 'revision', 'selection', canonical({'quote': key}), f'Instruction {number}', number))
            ctx.db.execute('INSERT INTO annotation_chats VALUES(?,?)', (key, current_chat(ctx)))
            ctx.db.execute('INSERT INTO task_annotations VALUES(?,?)', ('task', key))
        ctx.db.execute('INSERT INTO task_scopes VALUES(?,?,?,?,?)', ('task', 'report.docx', 'base', 'head', '[0,5]'))
    messages, _, _ = await context(ctx)
    constraints = json.loads(messages[0]['content'].split('Exact parent constraints and retained state: ', 1)[1])
    assert len(constraints['task_annotations']) == 20
    assert constraints['task_annotations'][-1]['comment'] == 'Instruction 19'
    assert constraints['edit_scope']['ranges'] == '[0,5]'


@pytest.mark.asyncio
async def test_clarifications_relevant_pairs_rejections_and_no_parent_schemas(ctx):
    ctx.message('user', TEXT, 'task')
    exchange(ctx, {'tool': 'word.load_tools', 'operations': ['prepare_creation']},
             {'loaded_operations': ['prepare_creation'], 'private_parent_schema': 'OMIT THIS'})
    question = exchange(ctx, {'tool': 'ask_user', 'text': 'Audience?'},
                        {'status': 'awaiting_user', 'text': 'Audience?'})
    ctx.message('user', 'Colleagues.', 'task')
    plan = exchange(ctx, {'tool': 'word.plan_draft', 'source_hash': '0' * 64,
                          'source_range': {'start': 's01', 'end': 's01'}},
                    {'code': 'objective_evidence_stale', 'message': 'Wrong hash.'})
    load = exchange(ctx, {'tool': 'word.load_tools', 'operations': ['inspect']},
                    {'code': 'context_capacity', 'message': 'Try one tool.'})
    ctx.message('assistant', 'malformed planning output', 'task')
    ctx.message('tool', '{"code":"invalid_schema","message":"Correct JSON."}', 'task')
    ctx.message('user', 'Use a neutral tone.', 'task')
    messages, _, _ = await context(ctx)
    assert [m['role'] for m in messages] == ['system', 'user', 'assistant', 'tool', 'user',
                                            'assistant', 'tool', 'assistant', 'tool', 'system', 'user']
    assert [m['tool_calls'][0]['id'] for m in messages if 'tool_calls' in m] == [question, plan, load]
    assert 'OMIT THIS' not in canonical(messages)
    assert 'untrusted source/tool evidence' in messages[-2]['content']
    assert messages[-1]['content'] == 'Use a neutral tone.'


@pytest.mark.asyncio
async def test_existing_draft_preserved_without_host_replanning(ctx):
    _, plan = native_plan(ctx)
    draft.submit_draft(ctx, 'task', ref=plan['ref'], text='Please review the document. 😀')
    before = list(ctx.db.iterdump())
    messages, tools, _ = await context(ctx)
    assert plan['ref'] in messages[0]['content'] and 'Please review the document. 😀' in messages[0]['content']
    assert 'explicit replanning decision' in messages[0]['content']
    assert tools[0]['function']['parameters']['properties']['source_range']['properties']['start']['enum'] == ['s01', 's02', 's03']
    assert list(ctx.db.iterdump()) == before


@pytest.mark.asyncio
async def test_unavailable_catalogue_returns_none_without_count(ctx):
    tools = tools_for(ctx)
    with ctx.transaction():
        ctx.db.execute('UPDATE tasks SET objective=? WHERE id=?', ('Sentence. ' * 25, 'task'))
    provider = Provider()
    assert await context(ctx, provider, tools=tools) is None
    assert not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['extra', 'missing', 'duplicate', 'hash', 'range', 'free_range'])
async def test_tool_contract_mismatch_rejected_without_mutation(ctx, change):
    tools = tools_for(ctx)
    if change == 'extra': tools.append(build('Draft', 'df_' + 'a' * 32)[1][0])
    elif change == 'missing': tools.pop()
    elif change == 'duplicate': tools.append(copy.deepcopy(tools[0]))
    elif change == 'hash': tools[0]['function']['parameters']['properties']['source_hash']['const'] = '0' * 64
    elif change == 'range': tools[0]['function']['parameters']['properties']['source_range']['properties']['start']['enum'] = ['s02']
    else: tools[0]['function']['parameters']['properties']['source_range']['properties']['end'] = {'type': 'string'}
    original = copy.deepcopy(tools)
    provider = Provider()
    with pytest.raises(HarnessError):
        await context(ctx, provider, tools=tools)
    assert tools == original and not provider.requests


@pytest.mark.asyncio
async def test_pending_native_call_blocks_even_unavailable_catalogue(ctx):
    tools = tools_for(ctx)
    record_call(ctx, 'task', {'tool': 'ask_user', 'text': 'Question?'})
    with ctx.transaction():
        ctx.db.execute('UPDATE tasks SET objective=? WHERE id=?', ('Sentence. ' * 25, 'task'))
    provider = Provider()
    with pytest.raises(HarnessError) as error:
        await context(ctx, provider, tools=tools)
    assert error.value.code == 'native_pending' and not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['chat_argument', 'message_chat', 'annotation_chat', 'raw_action', 'orphan'])
async def test_ownership_and_history_tamper_fail_closed(ctx, change):
    if change == 'chat_argument':
        with pytest.raises(HarnessError):
            await planning_context(ctx, Provider(), 'task', 'other', [], tools_for(ctx))
        return
    if change == 'annotation_chat':
        with ctx.transaction():
            ctx.db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?)', ('a', 'report.docx', 'r', 'all', '{}', 'Comment', 1))
            ctx.db.execute('INSERT INTO task_annotations VALUES(?,?)', ('task', 'a'))
    elif change == 'orphan': ctx.message('tool', '{"code":"bad","message":"bad"}', 'task')
    else:
        call = exchange(ctx, {'tool': 'ask_user', 'text': 'Question?'}, {'status': 'awaiting_user'})
        with ctx.transaction():
            if change == 'message_chat':
                ctx.db.execute('UPDATE message_chats SET chat_id=? WHERE message_id=(SELECT result_message FROM native_calls WHERE call_id=?)', ('other', call))
            else:
                ctx.db.execute('UPDATE messages SET content=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('{}', call))
    provider = Provider()
    with pytest.raises(HarnessError):
        await context(ctx, provider)
    assert not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [3200, 3201, True, -1])
async def test_token_limit_has_no_scope_trimming(ctx, count):
    before = list(ctx.db.iterdump())
    if count == 3200:
        assert (await context(ctx, Provider(count)))[2]['input_tokens'] == 3200
    else:
        with pytest.raises(HarnessError) as error:
            await context(ctx, Provider(count))
        assert error.value.code == 'context_capacity'
    assert list(ctx.db.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['decisions', 'scope', 'rows'])
async def test_exact_bounds_fail_instead_of_truncating(ctx, kind):
    decisions = []
    if kind == 'decisions': decisions = [{'text': 'x' * 65537}]
    elif kind == 'scope':
        with ctx.transaction():
            ctx.db.execute('INSERT INTO task_scopes VALUES(?,?,?,?,?)', ('task', 'report.docx', 'a', 'b', 'x' * 65537))
    else:
        for number in range(129): ctx.message('user', str(number), 'task')
    provider = Provider()
    with pytest.raises(HarnessError) as error:
        await context(ctx, provider, decisions)
    assert error.value.code == 'context_capacity' and not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['objective', 'target', 'clarification', 'tools', 'messages', 'pending_call'])
async def test_changes_during_token_count_fail_closed(ctx, change):
    class ChangingProvider(Provider):
        async def count_tokens(self, messages, *, tools):
            if change == 'clarification': ctx.message('user', 'New clarification.', 'task')
            elif change == 'pending_call': record_call(ctx, 'task', {'tool': 'ask_user', 'text': 'Question?'})
            elif change == 'tools': tools.pop()
            elif change == 'messages': messages[1]['content'] = 'altered'
            else:
                with ctx.transaction():
                    column = 'objective' if change == 'objective' else 'artifact'
                    ctx.db.execute(f'UPDATE tasks SET {column}=? WHERE id=?', ('Changed', 'task'))
            return 100
    with pytest.raises(HarnessError):
        await context(ctx, ChangingProvider())


@pytest.mark.asyncio
async def test_repeated_objective_after_first_message_is_retained_as_clarification(ctx):
    ctx.message('user', TEXT, 'task')
    ctx.message('user', TEXT, 'task')
    messages, _, _ = await context(ctx)
    assert len(messages) == 3 and messages[-1] == {'role': 'user', 'content': TEXT}


@pytest.mark.asyncio
async def test_generated_draft_changed_during_count_invalidates_snapshot(ctx):
    _, plan = native_plan(ctx)
    class ChangingProvider(Provider):
        async def count_tokens(self, messages, *, tools):
            draft.submit_draft(ctx, 'task', ref=plan['ref'], text='Please review the document.')
            return 100
    with pytest.raises(HarnessError) as error:
        await context(ctx, ChangingProvider())
    assert error.value.code == 'draft_stale'


@pytest.mark.asyncio
async def test_stale_retained_draft_cannot_be_silently_replanned(ctx):
    _, plan = native_plan(ctx)
    draft.submit_draft(ctx, 'task', ref=plan['ref'], text='Please review the document.')
    with ctx.transaction():
        ctx.db.execute('UPDATE tasks SET objective=? WHERE id=?', ('Different objective. Draft a request.', 'task'))
    provider = Provider()
    with pytest.raises(HarnessError) as error:
        await context(ctx, provider)
    assert error.value.code == 'draft_stale' and not provider.requests


def pending_load(store, operations=None):
    from localoffice.word.guidance import build_state, loaded_receipt
    operations = ['plan_draft'] if operations is None else operations
    call = record_call(store, 'task', {'tool': 'word.load_tools', 'operations': operations})
    return call, loaded_receipt(build_state(operations))


async def preflight(store, call, receipt, provider=None):
    return await planning_context(store, provider or Provider(), 'task', current_chat(store), [], tools_for(store),
                                  pending_tool_result=receipt, pending_call_id=call)


@pytest.mark.asyncio
async def test_preflight_counts_without_claiming_or_recording_load(ctx):
    ctx.message('user', TEXT, 'task')
    call, receipt = pending_load(ctx)
    before = list(ctx.db.iterdump())
    original = copy.deepcopy(receipt)
    provider = Provider()
    messages, tools, info = await preflight(ctx, call, receipt, provider)
    assert info['admission_only'] is True
    assert len(messages) == 2
    assert all('tool_calls' not in m and m['role'] != 'tool' for m in messages)
    assert call not in canonical(messages) and 'loaded_operations' not in canonical(messages)
    assert tools == tools_for(ctx) and provider.requests == [(messages, tools)]
    assert receipt == original and list(ctx.db.iterdump()) == before
    with pytest.raises(HarnessError) as error:
        await context(ctx)
    assert error.value.code == 'native_pending'
    # Only a real recorded result unlocks normal assembly, not the preflight.
    record_result(ctx, 'task', call, receipt)
    assert 'admission_only' not in (await context(ctx))[2]
    with pytest.raises(HarnessError):
        await preflight(ctx, call, receipt)


@pytest.mark.asyncio
async def test_preflight_receipt_uses_normalized_load_dependencies(ctx):
    call, receipt = pending_load(ctx, ['plan_draft', 'insert_image'])
    assert receipt == {'loaded_operations': ['plan_draft', 'insert_image', 'import_image']}
    assert (await preflight(ctx, call, receipt))[2]['admission_only'] is True
    with pytest.raises(HarnessError):
        await preflight(ctx, call, {'loaded_operations': ['plan_draft', 'insert_image']})


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_id', 'missing_receipt', 'wrong_id', 'fake_receipt',
    'extra_receipt_field', 'wrong_operations', 'extra_args', 'duplicate_operations', 'mutation',
    'raw_assistant', 'assistant_role', 'assistant_chat', 'assistant_task', 'call_chat',
    'not_latest', 'second_pending', 'no_pending'])
async def test_invalid_preflight_fails_without_writes_or_count(ctx, case):
    call, receipt = pending_load(ctx)
    if case == 'missing_id': call = None
    elif case == 'missing_receipt': receipt = None
    elif case == 'wrong_id': call = 'call_' + 'f' * 32
    elif case == 'fake_receipt': receipt = {'loaded_operations': ['inspect']}
    elif case == 'extra_receipt_field': receipt['executed'] = True
    elif case == 'not_latest': ctx.message('user', 'A later instruction.', 'task')
    elif case == 'second_pending':
        assistant = ctx.message('assistant', canonical({'tool': 'ask_user', 'text': 'Other call'}), 'task')
        with ctx.transaction():
            ctx.db.execute('INSERT INTO native_calls VALUES(?,?,?,?,?,NULL)',
                           ('call_' + 'f' * 32, 'task', current_chat(ctx), assistant,
                            canonical({'name': 'ask_user', 'arguments': {'text': 'Other call'}})))
    elif case == 'no_pending': record_result(ctx, 'task', call, receipt)
    else:
        with ctx.transaction():
            if case in ('wrong_operations', 'extra_args', 'duplicate_operations', 'mutation'):
                function = {'name': 'word.load_tools', 'arguments': {'operations': ['plan_draft']}}
                if case == 'wrong_operations': function['arguments']['operations'] = ['inspect']
                elif case == 'extra_args': function['arguments']['extra'] = True
                elif case == 'duplicate_operations': function['arguments']['operations'] *= 2
                else: function = {'name': 'word.create', 'arguments': {'path': 'report.docx'}}
                ctx.db.execute('UPDATE native_calls SET function=? WHERE call_id=?', (canonical(function), call))
                ctx.db.execute('UPDATE messages SET content=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)',
                               (canonical({'tool': function['name'], **function['arguments']}), call))
            elif case == 'raw_assistant':
                ctx.db.execute('UPDATE messages SET content=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('{}', call))
            elif case == 'assistant_role':
                ctx.db.execute('UPDATE messages SET role=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('user', call))
            elif case == 'assistant_chat':
                ctx.db.execute('UPDATE message_chats SET chat_id=? WHERE message_id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('other', call))
            elif case == 'assistant_task':
                ctx.db.execute('UPDATE messages SET task_id=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('other', call))
            elif case == 'call_chat': ctx.db.execute('UPDATE native_calls SET chat_id=? WHERE call_id=?', ('other', call))
    before = list(ctx.db.iterdump())
    provider = Provider()
    with pytest.raises(HarnessError):
        await preflight(ctx, call, receipt, provider)
    assert not provider.requests and list(ctx.db.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['receipt', 'arguments', 'settled', 'later_message'])
async def test_preflight_freshness_covers_call_request_and_proposed_result(ctx, change):
    call, receipt = pending_load(ctx)
    class ChangingProvider(Provider):
        async def count_tokens(self, messages, *, tools):
            if change == 'receipt': receipt['loaded_operations'].append('inspect')
            elif change == 'settled': record_result(ctx, 'task', call, receipt)
            elif change == 'later_message': ctx.message('user', 'Changed scope.', 'task')
            else:
                # Keep new arguments and proposed receipt mutually consistent:
                # only the immutable freshness stamp can reject this transition.
                request = {'tool': 'word.load_tools', 'operations': ['plan_draft', 'inspect']}
                receipt['loaded_operations'] = request['operations']
                with ctx.transaction():
                    ctx.db.execute('UPDATE native_calls SET function=? WHERE call_id=?',
                        (canonical({'name': request['tool'], 'arguments': {'operations': request['operations']}}), call))
                    ctx.db.execute('UPDATE messages SET content=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)',
                        (canonical(request), call))
            return 100
    with pytest.raises(HarnessError):
        await preflight(ctx, call, receipt, ChangingProvider())


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [3200, 3201])
async def test_preflight_admission_reserves_unchanged_budget(ctx, count):
    call, receipt = pending_load(ctx)
    before = list(ctx.db.iterdump())
    if count == 3200:
        assert (await preflight(ctx, call, receipt, Provider(count)))[2]['admission_only'] is True
    else:
        with pytest.raises(HarnessError) as error:
            await preflight(ctx, call, receipt, Provider(count))
        assert error.value.code == 'context_capacity'
    assert list(ctx.db.iterdump()) == before
