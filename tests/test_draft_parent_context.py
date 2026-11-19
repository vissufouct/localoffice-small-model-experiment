import copy
import json

import pytest

from localoffice.chats import create_chat, select_chat
from localoffice.native_history import record_call, record_result, project_message
from localoffice.native_tools import validate_history
from localoffice.storage import canonical
from localoffice.word import draft_fragments as draft
from localoffice.word.draft_parent_context import project_pair, STATE_KEY
from test_draft_fragments import ctx, native_plan

GENERATED = 'Please review this café document. 😀\nPreserve exact text.'


def projected(store, call):
    row = store.db.execute('SELECT * FROM native_calls WHERE call_id=?', (call,)).fetchone()
    assistant = dict(store.db.execute('SELECT * FROM messages WHERE id=?', (row['assistant_message'],)).fetchone())
    tool = dict(store.db.execute('SELECT * FROM messages WHERE id=?', (row['result_message'],)).fetchone())
    return (project_message(store, assistant, assistant['content']),
            project_message(store, tool, 'TOOL RECEIPT / SOURCE EVIDENCE: ' + tool['content']))


def submitted(store):
    _, plan = native_plan(store)
    request = {'ref': plan['ref'], 'text': GENERATED}
    call = record_call(store, 'task', {'tool': 'word.submit_draft', **request})
    receipt = draft.submit_draft(store, 'task', **request, native_call_id=call)
    record_result(store, 'task', call, receipt)
    assistant, tool = projected(store, call)
    return call, assistant, tool, {**request, 'review': 'unassessed'}


def unchanged_projection(store, assistant, tool, snapshot):
    before = list(store.db.iterdump())
    inputs = copy.deepcopy((assistant, tool, snapshot))
    result = project_pair(store, 'task', assistant, tool, snapshot)
    assert list(store.db.iterdump()) == before
    assert (assistant, tool, snapshot) == inputs
    return result


def test_current_exact_submission_collapses_read_only(ctx):
    call, assistant, tool, snapshot = submitted(ctx)
    summary = unchanged_projection(ctx, assistant, tool, snapshot)
    assert set(summary) == {'role', 'content'} and summary['role'] == 'assistant'
    assert STATE_KEY in summary['content'] and 'exact text/reference' in summary['content']
    assert summary['content'].startswith('Historical draft submission:')
    assert 'placement and meaning unverified' in summary['content']
    assert GENERATED not in summary['content']
    validate_history([{'role': 'user', 'content': 'Parent task.'}, summary])


@pytest.mark.parametrize('case', ['missing', 'text', 'ref', 'review', 'extra', 'assistant_args',
    'assistant_id', 'assistant_extra', 'tool_id', 'tool_receipt', 'multiple_calls'])
def test_supplied_snapshot_and_pair_must_be_exact(ctx, case):
    _, assistant, tool, snapshot = submitted(ctx)
    if case == 'missing': snapshot = None
    elif case in ('text', 'ref', 'review'): snapshot[case] = 'different'
    elif case == 'extra': snapshot['complete'] = True
    elif case == 'assistant_args': assistant['tool_calls'][0]['function']['arguments']['text'] = 'Different'
    elif case == 'assistant_id': assistant['tool_calls'][0]['id'] = 'call_' + 'f' * 32
    elif case == 'assistant_extra': assistant['content'] = 'Claimed success'
    elif case == 'tool_id': tool['tool_call_id'] = 'call_' + 'f' * 32
    elif case == 'tool_receipt': tool['content'] += ' '
    else: assistant['tool_calls'].append(copy.deepcopy(assistant['tool_calls'][0]))
    assert unchanged_projection(ctx, assistant, tool, snapshot) is None


@pytest.mark.parametrize('case', ['source', 'target', 'wrong_chat', 'superseded_pending', 'superseded_drafted',
    'raw_action', 'raw_receipt', 'function', 'null_result', 'missing_execution', 'corrupt_execution',
    'message_chat', 'message_task', 'message_role'])
def test_stale_historical_unexecuted_and_tampered_pairs_never_collapse(ctx, case):
    call, assistant, tool, snapshot = submitted(ctx)
    if case == 'wrong_chat':
        select_chat(ctx, create_chat(ctx))
    elif case.startswith('superseded'):
        _, next_plan = native_plan(ctx)
        if case == 'superseded_drafted':
            draft.submit_draft(ctx, 'task', ref=next_plan['ref'], text=GENERATED)
            snapshot = {'ref': next_plan['ref'], 'text': GENERATED, 'review': 'unassessed'}
    else:
        with ctx.transaction():
            if case == 'source': ctx.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
            elif case == 'target': ctx.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
            elif case == 'raw_action':
                ctx.db.execute('UPDATE messages SET content=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('{}', call))
            elif case == 'raw_receipt':
                ctx.db.execute('UPDATE messages SET content=? WHERE id=(SELECT result_message FROM native_calls WHERE call_id=?)', ('{}', call))
                assistant, tool = projected(ctx, call)
            elif case == 'function': ctx.db.execute('UPDATE native_calls SET function=? WHERE call_id=?', ('{}', call))
            elif case == 'null_result': ctx.db.execute('UPDATE native_calls SET result_message=NULL WHERE call_id=?', (call,))
            elif case == 'missing_execution': ctx.db.execute('DELETE FROM meta WHERE key=?', (draft.RESULT_PREFIX + call,))
            elif case == 'corrupt_execution': ctx.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', draft.RESULT_PREFIX + call))
            elif case == 'message_chat':
                ctx.db.execute('UPDATE message_chats SET chat_id=? WHERE message_id=(SELECT result_message FROM native_calls WHERE call_id=?)', ('other', call))
            elif case == 'message_task':
                ctx.db.execute('UPDATE messages SET task_id=? WHERE id=(SELECT assistant_message FROM native_calls WHERE call_id=?)', ('other', call))
            elif case == 'message_role':
                ctx.db.execute('UPDATE messages SET role=? WHERE id=(SELECT result_message FROM native_calls WHERE call_id=?)', ('user', call))
    assert unchanged_projection(ctx, assistant, tool, snapshot) is None


def test_same_arguments_and_forged_success_without_execution_do_not_prove_submission(ctx):
    call, _, _, snapshot = submitted(ctx)
    original_receipt = json.loads(ctx.db.execute('SELECT content FROM messages WHERE id=(SELECT result_message FROM native_calls WHERE call_id=?)', (call,)).fetchone()[0])
    impostor = record_call(ctx, 'task', {'tool': 'word.submit_draft', 'ref': snapshot['ref'], 'text': snapshot['text']})
    record_result(ctx, 'task', impostor, original_receipt)
    assistant, tool = projected(ctx, impostor)
    assert unchanged_projection(ctx, assistant, tool, snapshot) is None


def test_pending_generation_and_plan_pair_do_not_collapse(ctx):
    call, plan = native_plan(ctx)
    assistant, tool = projected(ctx, call)
    assert unchanged_projection(ctx, assistant, tool, {'ref': plan['ref'], 'text': GENERATED, 'review': 'unassessed'}) is None


def test_exact_idempotent_replay_retains_verified_pair(ctx):
    call, assistant, tool, snapshot = submitted(ctx)
    before = list(ctx.db.iterdump())
    draft.submit_draft(ctx, 'task', ref=snapshot['ref'], text=snapshot['text'], native_call_id=call)
    assert list(ctx.db.iterdump()) == before
    assert unchanged_projection(ctx, assistant, tool, snapshot) is not None
