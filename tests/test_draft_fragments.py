"""Offline draft provenance and atomicity; no workers or inference."""
import re

import pytest
from pydantic import ValidationError

from localoffice.chats import current_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.storage import ProjectStore, digest
from localoffice.word import draft_fragments as draft

TEXT = 'Keep this literal café. Draft an invitation 😀 to review. Preserve the ending.'


@pytest.fixture
def ctx(tmp_path):
    store = ProjectStore(tmp_path)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('task', TEXT, 'running', 'background', 'report.docx', 1, 1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(store)))
    yield store
    store.close()


def args(text=TEXT):
    return {'source_hash': digest(text.encode()), 'source_range': {'start': 's02', 'end': 's02'}}


def snapshot(store):
    return [tuple(row) for row in store.db.execute('SELECT key,value FROM meta ORDER BY key')]


def change_source(store, text='A changed source.'):
    with store.transaction():
        store.db.execute('UPDATE tasks SET objective=? WHERE id=?', (text, 'task'))


def native_plan(store):
    call = record_call(store, 'task', {'tool': 'word.plan_draft', **args()})
    receipt = draft.plan_draft(store, 'task', **args(), native_call_id=call)
    record_result(store, 'task', call, receipt)
    return call, receipt


def test_exact_instruction_pending_and_drafted_separate_no_operations(ctx):
    receipt = draft.plan_draft(ctx, 'task', **args())
    state = draft.pending(ctx, 'task')
    assert re.fullmatch(r'df_[0-9a-f]{32}', receipt['ref'])
    assert state['instruction'] == 'Draft an invitation 😀 to review. '
    assert state['source_text'] == TEXT
    span = state['source_span']
    assert TEXT[span['start']:span['end']] == state['instruction']
    assert state['request'] == {'tool': 'word.plan_draft', **args()}
    assert state['owner'] == {'task_id': 'task', 'chat_id': current_chat(ctx), 'target': 'report.docx'}
    assert 'text' not in state
    before = draft._read(ctx, draft._key('task', receipt['ref'], 'pending'))
    generated = 'Por favor, reveja o documento. 😀\nCafé e é \\ "test"'
    done = draft.submit_draft(ctx, 'task', ref=receipt['ref'], text=generated)
    assert done['status'] == 'drafted' and done['semantic_validation'] == 'unassessed'
    assert draft.pending(ctx, 'task') is None
    assert draft.get(ctx, 'task')['text'] == generated
    assert draft._read(ctx, draft._key('task', receipt['ref'], 'pending')) == before
    assert ctx.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert ctx.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0] == 0
    assert not (ctx.root / 'report.docx').exists()


def test_same_arguments_new_call_new_generation_and_old_replay(ctx):
    call, first = native_plan(ctx)
    second_call = record_call(ctx, 'task', {'tool': 'word.plan_draft', **args()})
    assert draft.result(ctx, 'task', args(), native_call_id=second_call) is None
    second = draft.plan_draft(ctx, 'task', **args(), native_call_id=second_call)
    assert second['generation'] == 2 and second['supersedes_generation'] == 1
    assert first['ref'] != second['ref']
    active = draft.get(ctx, 'task')
    assert active['supersedes']['ref'] == first['ref']
    change_source(ctx)
    before = snapshot(ctx)
    assert draft.result(ctx, 'task', args(), native_call_id=call) == first
    assert draft.plan_draft(ctx, 'task', **args(), native_call_id=call) == first
    assert draft.get(ctx, 'task') == active and snapshot(ctx) == before
    with pytest.raises(HarnessError):
        draft.submit_draft(ctx, 'task', ref=first['ref'], text='Old generation')


def test_submit_exact_replay_after_source_and_target_change(ctx):
    _, first = native_plan(ctx)
    request = {'tool': 'word.submit_draft', 'ref': first['ref'], 'text': 'Please review.'}
    call = record_call(ctx, 'task', request)
    receipt = draft.submit_draft(ctx, 'task', ref=first['ref'], text=request['text'], native_call_id=call)
    record_result(ctx, 'task', call, receipt)
    draft.plan_draft(ctx, 'task', **args())
    change_source(ctx)
    with ctx.transaction():
        ctx.db.execute("UPDATE tasks SET artifact='another.docx' WHERE id='task'")
    before = snapshot(ctx)
    assert draft.result(ctx, 'task', request, native_call_id=call) == receipt
    assert draft.submit_draft(ctx, 'task', ref=first['ref'], text=request['text'], native_call_id=call) == receipt
    assert snapshot(ctx) == before
    with pytest.raises(HarnessError):
        draft.submit_draft(ctx, 'task', ref=first['ref'], text=request['text'])


@pytest.mark.parametrize('change', ['source', 'target', 'task_chat', 'other_task'])
def test_scope_and_staleness_atomic(ctx, change):
    receipt = draft.plan_draft(ctx, 'task', **args())
    task_id = 'task'
    if change == 'source':
        change_source(ctx)
    elif change == 'target':
        with ctx.transaction():
            ctx.db.execute("UPDATE tasks SET artifact='another.docx' WHERE id='task'")
    elif change == 'task_chat':
        with ctx.transaction():
            ctx.db.execute("DELETE FROM task_chats WHERE task_id='task'")
    else:
        task_id = 'other'
        with ctx.transaction():
            ctx.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                           ('other', TEXT, 'running', 'background', 'report.docx', 1, 1))
            ctx.db.execute('INSERT INTO task_chats VALUES(?,?)', ('other', current_chat(ctx)))
    before = snapshot(ctx)
    with pytest.raises(HarnessError):
        draft.submit_draft(ctx, task_id, ref=receipt['ref'], text='Draft text')
    assert snapshot(ctx) == before


def test_conflicting_overwrite_and_same_text_retry(ctx):
    first = draft.plan_draft(ctx, 'task', **args())
    receipt = draft.submit_draft(ctx, 'task', ref=first['ref'], text='First text')
    before = snapshot(ctx)
    assert draft.submit_draft(ctx, 'task', ref=first['ref'], text='First text') == receipt
    with pytest.raises(HarnessError) as exc:
        draft.submit_draft(ctx, 'task', ref=first['ref'], text='Competing text')
    assert exc.value.code == 'draft_conflict' and snapshot(ctx) == before
    change_source(ctx)
    with pytest.raises(HarnessError):
        draft.submit_draft(ctx, 'task', ref=first['ref'], text='First text')


@pytest.mark.parametrize('text', ['', ' \n\t', 'x' * 8001, '\ud800', 42])
def test_invalid_generated_text_atomic(ctx, text):
    first = draft.plan_draft(ctx, 'task', **args())
    before = snapshot(ctx)
    with pytest.raises(ValidationError):
        draft.submit_draft(ctx, 'task', ref=first['ref'], text=text)
    assert snapshot(ctx) == before


def test_unicode_character_limit_not_bytes(ctx):
    first = draft.plan_draft(ctx, 'task', **args())
    text = '😀' * 8000
    draft.submit_draft(ctx, 'task', ref=first['ref'], text=text)
    assert draft.get(ctx, 'task')['text'] == text


@pytest.mark.parametrize('patch', [
    {'source_hash': '0' * 64},
    {'source_range': {'start': 's03', 'end': 's01'}},
    {'source_range': {'start': 's24', 'end': 's24'}},
    {'source_range': {'start': 's01', 'end': 's02', 'extra': True}},
    {'source_range': {'start': 's1', 'end': 's1'}},
])
def test_invalid_source_range_atomic(ctx, patch):
    before = snapshot(ctx)
    with pytest.raises((HarnessError, ValidationError)):
        draft.plan_draft(ctx, 'task', **{**args(), **patch})
    assert snapshot(ctx) == before


def test_closed_models():
    for model, values in [(draft.PlanDraft, args()),
                          (draft.SubmitDraft, {'ref': 'df_' + 'a' * 32, 'text': 'Text'})]:
        assert model.model_json_schema()['additionalProperties'] is False
        with pytest.raises(ValidationError):
            model(**values, native_call_id='call_' + 'a' * 32)


@pytest.mark.parametrize('stage', ['history', 'pointer', 'receipt'])
def test_failed_plan_write_rolls_back(ctx, monkeypatch, stage):
    call = record_call(ctx, 'task', {'tool': 'word.plan_draft', **args()})
    before = snapshot(ctx)
    original = draft._write
    prefix = {'history': draft.HISTORY_PREFIX, 'pointer': draft.PREFIX, 'receipt': draft.RESULT_PREFIX}[stage]

    def fail(store, key, state):
        original(store, key, state)
        if key.startswith(prefix):
            raise RuntimeError('injected failure')

    monkeypatch.setattr(draft, '_write', fail)
    with pytest.raises(RuntimeError):
        draft.plan_draft(ctx, 'task', **args(), native_call_id=call)
    assert snapshot(ctx) == before


def test_failed_submit_receipt_rolls_back(ctx, monkeypatch):
    _, first = native_plan(ctx)
    request = {'tool': 'word.submit_draft', 'ref': first['ref'], 'text': 'Draft'}
    call = record_call(ctx, 'task', request)
    before = snapshot(ctx)
    original = draft._write

    def fail(store, key, state):
        original(store, key, state)
        if key.startswith(draft.RESULT_PREFIX):
            raise RuntimeError('injected failure')

    monkeypatch.setattr(draft, '_write', fail)
    with pytest.raises(RuntimeError):
        draft.submit_draft(ctx, 'task', ref=first['ref'], text='Draft', native_call_id=call)
    assert snapshot(ctx) == before


@pytest.mark.parametrize('field,value', [('instruction', 'Invented'), ('source_span', {'start': 0, 'end': 2}),
                                         ('source_version', 2), ('request_hash', '0' * 64), ('version', True)])
def test_rehashed_tamper_rejected(ctx, field, value):
    first = draft.plan_draft(ctx, 'task', **args())
    key = draft._key('task', first['ref'], 'pending')
    state = draft._read(ctx, key)
    state[field] = value
    with ctx.transaction():
        ctx.db.execute('DELETE FROM meta WHERE key IN (?,?)', (key, draft.PREFIX + 'task'))
        draft._write(ctx, key, state)
        draft._write(ctx, draft.PREFIX + 'task', {'ref': first['ref'], 'status': 'pending', 'state_hash': draft._hash(state)})
    with pytest.raises(HarnessError) as exc:
        draft.get(ctx, 'task')
    assert exc.value.code == 'state_corrupt'


def test_exact_identity_mismatch_completed_without_receipt_and_tamper(ctx):
    call, receipt = native_plan(ctx)
    with pytest.raises(HarnessError):
        draft.result(ctx, 'task', {**args(), 'source_range': {'start': 's01', 'end': 's01'}}, native_call_id=call)
    second = record_call(ctx, 'task', {'tool': 'word.plan_draft', **args()})
    assert draft.result(ctx, 'task', args(), native_call_id=second) is None
    record_result(ctx, 'task', second, receipt)
    with pytest.raises(HarnessError):
        draft.plan_draft(ctx, 'task', **args(), native_call_id=second)
    with ctx.transaction():
        ctx.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', draft.RESULT_PREFIX + call))
    with pytest.raises(HarnessError):
        draft.result(ctx, 'task', args(), native_call_id=call)


def test_persistence_and_no_record(tmp_path):
    store = ProjectStore(tmp_path)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('task', TEXT, 'running', 'background', 'report.docx', 1, 1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(store)))
    assert draft.get(store, 'task') is None and draft.pending(store, 'task') is None
    first = draft.plan_draft(store, 'task', **args())
    store.close()
    other = ProjectStore(tmp_path)
    try:
        assert draft.get(other, 'task')['ref'] == first['ref']
    finally:
        other.close()


def test_submit_new_call_same_text_requires_actual_execution(ctx):
    first = draft.plan_draft(ctx, 'task', **args())
    request = {'tool': 'word.submit_draft', 'ref': first['ref'], 'text': 'Review please.'}
    draft.submit_draft(ctx, 'task', ref=first['ref'], text=request['text'])
    call = record_call(ctx, 'task', request)
    assert draft.result(ctx, 'task', request, native_call_id=call) is None
    receipt = draft.submit_draft(ctx, 'task', ref=first['ref'], text=request['text'], native_call_id=call)
    assert draft.result(ctx, 'task', request, native_call_id=call) == receipt
    with pytest.raises(HarnessError):
        draft.submit_draft(ctx, 'task', ref=first['ref'], text='Changed', native_call_id=call)


def test_unavailable_catalogue_and_target(ctx):
    change_source(ctx, 'x' * 3000)
    before = snapshot(ctx)
    with pytest.raises(HarnessError):
        draft.plan_draft(ctx, 'task', **args('x' * 3000))
    assert snapshot(ctx) == before
    change_source(ctx, TEXT)
    with ctx.transaction():
        ctx.db.execute("UPDATE tasks SET artifact='sheet.xlsx' WHERE id='task'")
    with pytest.raises(HarnessError):
        draft.plan_draft(ctx, 'task', **args())


def test_predecessor_and_drafted_tamper_rejected(ctx):
    first = draft.plan_draft(ctx, 'task', **args())
    draft.submit_draft(ctx, 'task', ref=first['ref'], text='Please review.')
    draft.plan_draft(ctx, 'task', **args())
    key = draft._key('task', first['ref'], 'drafted')
    state = draft._read(ctx, key)
    state['text'] = 'Different text'
    with ctx.transaction():
        ctx.db.execute('DELETE FROM meta WHERE key=?', (key,))
        draft._write(ctx, key, state)
    with pytest.raises(HarnessError):
        draft.get(ctx, 'task')
