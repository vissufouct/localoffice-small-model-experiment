"""Reconcile retained proposal revisions without reapplying patches or workers."""
import json

import pytest

from localoffice.errors import HarnessError
from localoffice.native_history import record_call, reconcile_word_call
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word.creation_repair import PREFIX, get
from localoffice.word.plugin import WordPlugin
from test_creation_proposal_recovery import (
    store, proposal, prepare, meta, pending_result, no_execution,
)


def revision(ref, size=10):
    return {'tool': 'word.revise_creation', 'ref': ref,
            'changes': [{'op': 'set', 'path': 'styles.0.size_points', 'value': size}]}


def revise(store, request):
    return WordPlugin(store).revise_creation(task_id='task',
        **{key: value for key, value in request.items() if key != 'tool'})


def forbid_reexecution(monkeypatch):
    no_execution(monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail('Recovery must never reapply a retained proposal revision')
    monkeypatch.setattr(WordPlugin, 'revise_creation', forbidden)


@pytest.mark.parametrize('change', [
    {'op': 'set', 'path': 'styles.0.size_points', 'value': 10},
    {'op': 'remove', 'path': 'footer'},
    {'op': 'insert', 'path': 'blocks.1', 'value': {'kind': 'paragraph', 'text': 'Added paragraph'}},
])
def test_exact_revision_recovers_after_restart_without_reapplication(store, monkeypatch, change):
    from localoffice.word.contracts import ReviseCreation
    prepared = prepare(store, proposal())
    request = {**revision(prepared['ref']), 'changes': [change]}
    call = record_call(store, 'task', request)
    revised = revise(store, request)
    assert revised['ref'] != prepared['ref']
    state = get(store, 'task')
    expected = ReviseCreation.model_validate(request).model_dump(exclude={'tool'})
    assert state['revision_origin']['ref'] == prepared['ref']
    assert state['revision_origin']['request_hash'] == digest(canonical(expected).encode())
    before = meta(store)
    root = store.root
    store.close()
    reopened = ProjectStore(root)
    try:
        forbid_reexecution(monkeypatch)
        result = reconcile_word_call(reopened, 'task')
        assert result['state'] == 'reconciled' and result['ref'] == revised['ref']
        receipt = json.loads(reopened.db.execute('SELECT content FROM messages WHERE id=?',
                                                (result['message_id'],)).fetchone()[0])
        assert receipt['status'] == 'prepared'
        assert receipt['ref'] == revised['ref'] and receipt['document_changed'] is False
        assert pending_result(reopened, call) == result['message_id']
        assert meta(reopened) == before
        assert reopened.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        assert not reopened.candidate('report.docx')
        assert not (root / 'report.docx').exists()
        assert reconcile_word_call(reopened, 'task')['state'] == 'no_pending_call'
    finally:
        reopened.close()


@pytest.mark.parametrize('saved_prepare', [False, True])
def test_unexecuted_revision_cannot_be_inferred(store, monkeypatch, saved_prepare):
    prepared = prepare(store, proposal()) if saved_prepare else {'ref': 'cr_' + 'a' * 32}
    call = record_call(store, 'task', revision(prepared['ref']))
    forbid_reexecution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert pending_result(store, call) is None


@pytest.mark.parametrize('replacement', ['prepare', 'revise'])
def test_superseded_revision_remains_unresolved(store, monkeypatch, replacement):
    prepared = prepare(store, proposal())
    request = revision(prepared['ref'])
    call = record_call(store, 'task', request)
    revised = revise(store, request)
    if replacement == 'revise':
        revise(store, revision(revised['ref'], 12))
    else:
        new = proposal()
        new['blocks'][0]['text'] = 'Replacement proposal'
        prepare(store, new)
    before = meta(store)
    forbid_reexecution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert pending_result(store, call) is None and meta(store) == before


@pytest.mark.parametrize('stale', ['objective', 'target'])
def test_stale_revised_proposal_is_not_recovered(store, monkeypatch, stale):
    prepared = prepare(store, proposal())
    request = revision(prepared['ref'])
    call = record_call(store, 'task', request)
    revise(store, request)
    before = meta(store)
    if stale == 'objective':
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective='Changed source' WHERE id='task'")
    else:
        (store.root / 'report.docx').write_bytes(b'Existing external target')
    forbid_reexecution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert pending_result(store, call) is None and meta(store) == before


@pytest.mark.parametrize('mismatch', ['changes', 'ref', 'review_required'])
def test_revision_identity_and_review_requirement_are_not_inferred(store, monkeypatch, mismatch):
    prepared = prepare(store, proposal())
    request = revision(prepared['ref'])
    call = record_call(store, 'task', request)
    revise(store, request)
    if mismatch == 'changes':
        row = store.db.execute('SELECT function FROM native_calls WHERE call_id=?', (call,)).fetchone()
        function = json.loads(row[0])
        function['arguments']['changes'][0]['value'] = 11
        with store.transaction():
            store.db.execute('UPDATE native_calls SET function=? WHERE call_id=?', (canonical(function), call))
    else:
        envelope = json.loads(meta(store))
        if mismatch == 'ref':
            envelope['state']['revision_origin']['ref'] = 'cr_' + 'b' * 32
        else:
            envelope['state']['review_required'] = False
        envelope['hash'] = digest(canonical(envelope['state']).encode())
        with store.transaction():
            store.db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(envelope), PREFIX + 'task'))
    before = meta(store)
    forbid_reexecution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert pending_result(store, call) is None and meta(store) == before


def test_tampered_revision_origin_fails_original_integrity_check(store, monkeypatch):
    prepared = prepare(store, proposal())
    request = revision(prepared['ref'])
    call = record_call(store, 'task', request)
    revise(store, request)
    envelope = json.loads(meta(store))
    envelope['state']['revision_origin']['request_hash'] = '0' * 64
    with store.transaction():
        store.db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(envelope), PREFIX + 'task'))
    before = meta(store)
    forbid_reexecution(monkeypatch)
    with pytest.raises(HarnessError) as exc:
        reconcile_word_call(store, 'task')
    assert exc.value.code == 'state_corrupt'
    assert pending_result(store, call) is None and meta(store) == before


def test_cross_chat_revision_recovery_cannot_consume_pending_call(store, monkeypatch):
    prepared = prepare(store, proposal())
    request = revision(prepared['ref'])
    call = record_call(store, 'task', request)
    revise(store, request)
    before = meta(store)
    forbid_reexecution(monkeypatch)
    monkeypatch.setattr('localoffice.native_history.current_chat', lambda _: 'other-chat')
    with pytest.raises(HarnessError) as exc:
        reconcile_word_call(store, 'task')
    assert exc.value.code == 'chat_scope'
    assert pending_result(store, call) is None and meta(store) == before
