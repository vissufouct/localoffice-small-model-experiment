"""Draft dispatch and crash recovery stay metadata-only and exact-call scoped."""
import json

import pytest

from localoffice.chats import current_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result, reconcile_word_call
from localoffice.plugins import first_party_registry
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word import draft_fragments as draft
from localoffice.word.plugin import WordPlugin

TEXT = 'Keep the supplied text. Draft a short invitation to review.'


@pytest.fixture
def store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Draft dispatch/recovery must not invoke a document worker.')

    monkeypatch.setattr(WordPlugin, '_execute', forbidden)
    monkeypatch.setattr('localoffice.word.plugin.subprocess.run', forbidden)
    instance = ProjectStore(tmp_path)
    with instance.transaction():
        instance.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                            ('task', TEXT, 'running', 'background', 'report.docx', 1, 1))
        instance.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(instance)))
    yield instance
    assert instance.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert instance.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0] == 0
    assert not [p for p in tmp_path.rglob('*') if p.is_file() and '.localoffice' not in p.parts]
    instance.close()


def plan_args():
    return {'source_hash': digest(TEXT.encode()), 'source_range': {'start': 's02', 'end': 's02'}}


def action(store, kind):
    if kind == 'plan_draft':
        return {'tool': 'word.plan_draft', **plan_args()}
    planned = draft.plan_draft(store, 'task', **plan_args())
    return {'tool': 'word.submit_draft', 'ref': planned['ref'], 'text': 'Please review this document. 😀'}


def execute(store, request, call_id=None):
    arguments = {k: v for k, v in request.items() if k != 'tool'}
    method = getattr(draft, request['tool'].split('.')[1])
    return method(store, 'task', **arguments, native_call_id=call_id)


def metadata(store):
    return [tuple(row) for row in store.db.execute('SELECT * FROM meta ORDER BY key')]


@pytest.mark.asyncio
async def test_registry_dispatch_both_tools_and_host_identity(store, monkeypatch):
    registry = first_party_registry(store)
    assert {'word.plan_draft', 'word.submit_draft'} <= set(registry.plugins['word'].capabilities)
    calls = []
    original = registry.word.call_async

    async def tracked(name, **arguments):
        calls.append((name, arguments.copy()))
        return await original(name, **arguments)

    monkeypatch.setattr(registry.word, 'call_async', tracked)
    planned = None
    for name in ('plan_draft', 'submit_draft'):
        arguments = plan_args() if name == 'plan_draft' else {'ref': planned['ref'], 'text': 'Please review.'}
        request = {'tool': 'word.' + name, **arguments}
        call_id = record_call(store, 'task', request)
        receipt = await registry.call(request['tool'], task_id='task', native_call_id=call_id, **arguments)
        assert calls[-1] == (name, {'task_id': 'task', 'native_call_id': call_id, **arguments})
        assert draft.result(store, 'task', request, native_call_id=call_id) == receipt
        record_result(store, 'task', call_id, receipt)
        if name == 'plan_draft':
            planned = receipt
            assert draft.pending(store, 'task')['instruction'] == 'Draft a short invitation to review.'
        else:
            assert draft.get(store, 'task')['text'] == 'Please review.'
    assert not registry.word.busy


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['plan_draft', 'submit_draft'])
async def test_disabled_registry_refuses_without_writes_then_reenables(store, kind):
    request = action(store, kind)
    registry = first_party_registry(store)
    registry.set_enabled('word', False)
    before = metadata(store)
    with pytest.raises(HarnessError) as exc:
        await registry.call(request['tool'], task_id='task', **{k: v for k, v in request.items() if k != 'tool'})
    assert exc.value.code == 'plugin_disabled' and metadata(store) == before
    registry.set_enabled('word', True)
    receipt = await registry.call(request['tool'], task_id='task', **{k: v for k, v in request.items() if k != 'tool'})
    assert receipt['status'] == ('pending' if kind == 'plan_draft' else 'drafted')


@pytest.mark.parametrize('kind', ['plan_draft', 'submit_draft'])
@pytest.mark.parametrize('historical', [False, True])
def test_interrupted_completed_call_recovers_exactly_preserving_other_state(store, kind, historical):
    request = action(store, kind)
    call_id = record_call(store, 'task', request)
    receipt = execute(store, request, call_id)
    if historical:
        draft.plan_draft(store, 'task', **plan_args())
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective='Changed source.',artifact='other.docx' WHERE id='task'")
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         ('task_recovery:task', canonical({'unresolved': True, 'mutation': True,
                                                          'reason': 'unrelated repair', 'version': 1})))
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         ('word_tools:task', canonical({'operations': ['inspect']})))
    before = metadata(store)
    recovered = reconcile_word_call(store, 'task')
    assert recovered['state'] == 'reconciled' and recovered['call_id'] == call_id
    assert recovered['ref'] == receipt['ref']
    message = store.db.execute('SELECT role,content FROM messages WHERE id=?', (recovered['message_id'],)).fetchone()
    assert message['role'] == 'tool' and json.loads(message['content']) == receipt
    assert metadata(store) == before
    assert reconcile_word_call(store, 'task') == {'state': 'no_pending_call'}


@pytest.mark.parametrize('kind', ['plan_draft', 'submit_draft'])
def test_identical_unexecuted_call_never_recovers_or_executes(store, kind):
    request = action(store, kind)
    first = record_call(store, 'task', request)
    receipt = execute(store, request, first)
    record_result(store, 'task', first, receipt)
    second = record_call(store, 'task', request)
    before = metadata(store)
    message_count = store.db.execute('SELECT count(*) FROM messages').fetchone()[0]
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert draft.result(store, 'task', request, native_call_id=second) is None
    assert metadata(store) == before
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == message_count
    assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?', (second,)).fetchone()[0] is None


@pytest.mark.parametrize('kind', ['plan_draft', 'submit_draft'])
def test_legacy_matching_execution_without_call_receipt_not_recovered(store, kind):
    request = action(store, kind)
    execute(store, request)
    record_call(store, 'task', request)
    before = metadata(store)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert metadata(store) == before


@pytest.mark.parametrize('kind', ['plan_draft', 'submit_draft'])
def test_tampered_receipt_fails_without_recording_result(store, kind):
    request = action(store, kind)
    call_id = record_call(store, 'task', request)
    execute(store, request, call_id)
    key = draft.RESULT_PREFIX + call_id
    with store.transaction():
        saved = json.loads(store.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
        saved['state']['receipt']['ref'] = 'df_' + '0' * 32
        store.db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(saved), key))
    before = metadata(store)
    with pytest.raises(HarnessError) as exc:
        reconcile_word_call(store, 'task')
    assert exc.value.code == 'state_corrupt'
    assert metadata(store) == before
    assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?', (call_id,)).fetchone()[0] is None
