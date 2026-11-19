"""Native recovery of reviewed proposals and exact finalization receipts."""
import json

import pytest

from localoffice.errors import HarnessError
from localoffice.native_history import operation_key, record_call, reconcile_word_call
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word.content_plan import plan
from localoffice.word.creation_repair import PREFIX, get
from localoffice.word.plugin import WordPlugin
from test_creation_repair_runtime import REQUEST, STYLES
from test_word_progress import task


@pytest.fixture
def store(tmp_path):
    value = ProjectStore(tmp_path)
    task(value)
    with value.transaction():
        value.db.execute("UPDATE tasks SET objective=? WHERE id='task'", ('Exact body; Exact header; Page ; Sheet ',))
    try:
        yield value
    finally:
        value.close()


def proposal(valid=True):
    value = json.loads(json.dumps(REQUEST))
    value['tool'] = 'word.prepare_creation'
    if valid:
        value['styles'] = STYLES
    return value


def prepare(store, request, native_call_id=None):
    return WordPlugin(store).prepare_creation(task_id='task', native_call_id=native_call_id, **{k: v for k, v in request.items() if k != 'tool'})


def meta(store):
    return store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + 'task',)).fetchone()[0]


def no_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Recovery must not capture, resolve, or execute again')
    monkeypatch.setattr(WordPlugin, '_execute', forbidden)
    monkeypatch.setattr('localoffice.word.creation_repair.capture', forbidden)
    monkeypatch.setattr('localoffice.word.creation_repair.resolve', forbidden)


def pending_result(store, call):
    return store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?', (call,)).fetchone()[0]


@pytest.mark.parametrize('valid', [False, True])
def test_prepare_recovers_exact_proposal_after_restart_without_mutating_metadata(store, monkeypatch, valid):
    request = proposal(valid)
    call = record_call(store, 'task', request)
    prepared = prepare(store, request, native_call_id=call)
    original = meta(store)
    assert get(store, 'task')['review_required'] is True
    root = store.root
    store.close()
    reopened = ProjectStore(root)
    try:
        no_execution(monkeypatch)
        result = reconcile_word_call(reopened, 'task')
        assert result['state'] == 'reconciled'
        assert result['call_id'] == call
        message = reopened.db.execute('SELECT content FROM messages WHERE id=?', (result['message_id'],)).fetchone()[0]
        receipt = json.loads(message)
        assert receipt['ref'] == prepared['ref']
        assert receipt['document_changed'] is False
        assert meta(reopened) == original
        assert reopened.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        assert not reopened.candidate('report.docx')
        assert not (root / 'report.docx').exists()
        assert reconcile_word_call(reopened, 'task')['state'] == 'no_pending_call'
    finally:
        reopened.close()


def test_prepare_without_durable_capture_is_not_inferred(store, monkeypatch):
    call = record_call(store, 'task', proposal())
    no_execution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert pending_result(store, call) is None


def test_replaced_prepare_recovers_historical_receipt_without_routing(store, monkeypatch):
    request = proposal()
    call = record_call(store, 'task', request)
    first = prepare(store, request, native_call_id=call)
    request['blocks'][0]['text'] = 'A different proposal'
    second = prepare(store, request)
    assert second['ref'] != first['ref']
    original = meta(store)
    no_execution(monkeypatch)
    recovered=reconcile_word_call(store, 'task')
    assert recovered['state']=='reconciled' and recovered['routing_restored'] is False
    saved=json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(recovered['message_id'],)).fetchone()[0])
    assert saved==first
    assert meta(store) == original


@pytest.mark.parametrize('stale', ['objective', 'target'])
def test_stale_prepare_recovers_historical_receipt_without_routing(store, monkeypatch, stale):
    request = proposal()
    call = record_call(store, 'task', request)
    prepare(store, request, native_call_id=call)
    original = meta(store)
    if stale == 'objective':
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective='Changed objective' WHERE id='task'")
    else:
        (store.root / 'report.docx').write_bytes(b'External target now exists')
    no_execution(monkeypatch)
    recovered=reconcile_word_call(store, 'task')
    assert recovered['state']=='reconciled' and recovered['routing_restored'] is False
    assert meta(store) == original


@pytest.mark.parametrize('tamper', ['envelope', 'payload'])
def test_prepare_original_hashes_verified_before_identity_matching(store, monkeypatch, tamper):
    request = proposal()
    call = record_call(store, 'task', request)
    prepare(store, request, native_call_id=call)
    envelope = json.loads(meta(store))
    if tamper == 'envelope':
        envelope['hash'] = '0' * 64
    else:
        envelope['state']['payload']['blocks'][0]['text'] = 'Tampered'
        # Even a matching outer hash must not hide a stale original-payload hash.
        envelope['hash'] = digest(canonical(envelope['state']).encode())
    with store.transaction():
        store.db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(envelope), PREFIX + 'task'))
    original = meta(store)
    no_execution(monkeypatch)
    with pytest.raises(HarnessError) as exc:
        reconcile_word_call(store, 'task')
    assert exc.value.code == 'state_corrupt'
    assert pending_result(store, call) is None
    assert meta(store) == original


def finalize(store, request, key):
    return WordPlugin(store).finalize_creation(task_id='task', idempotency_key=key,
        **{k: v for k, v in request.items() if k != 'tool'})


def test_finalize_recovers_exact_ledger_after_plan_replace_no_rebinding(store, monkeypatch):
    refs = plan(store, 'task', literals=[{'role': 'footer', 'quote': 'Page '},
                                       {'role': 'footer', 'quote': 'Sheet '}])['literals']
    prepared = prepare(store, proposal())
    request = {'tool': 'word.finalize_creation', 'ref': prepared['ref'],
               'source_bindings': {'footer.text': refs[0]['ref']}}
    call = record_call(store, 'task', request)
    receipt = finalize(store, request, operation_key(call))
    original = dict(store.operation(receipt['receipt_id']))
    saved = json.loads(original['args'])
    assert saved['repair_request']['tool'] == 'word.finalize_creation'
    assert saved['request']['footer']['text'] == 'Page '
    plan(store, 'task', plan_mode='replace', literals=[{'role': 'footer', 'quote': 'Sheet '}])
    no_execution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'reconciled'
    assert finalize(store, request, operation_key(call)) == receipt
    with pytest.raises(HarnessError) as exc:
        finalize(store, {**request, 'source_bindings': {'footer.text': refs[1]['ref']}}, operation_key(call))
    assert exc.value.code == 'idempotency_conflict'
    assert dict(store.operation(receipt['receipt_id'])) == original
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 1


@pytest.mark.parametrize('valid_hash', [False, True])
def test_finalize_missing_empty_map_compatibility_follows_original_hash_check(store, monkeypatch, valid_hash):
    prepared = prepare(store, proposal())
    request = {'tool': 'word.finalize_creation', 'ref': prepared['ref'], 'source_bindings': {}}
    call = record_call(store, 'task', request)
    receipt = finalize(store, request, operation_key(call))
    original = dict(store.operation(receipt['receipt_id']))
    saved = json.loads(original['args'])
    assert saved['repair_request'].pop('source_bindings') == {}
    encoded = canonical(saved)
    with store.transaction():
        store.db.execute('UPDATE operations SET args=?,args_hash=? WHERE id=?',
            (encoded, digest(encoded.encode()) if valid_hash else original['args_hash'], receipt['receipt_id']))
    before = dict(store.operation(receipt['receipt_id']))
    no_execution(monkeypatch)
    if valid_hash:
        assert reconcile_word_call(store, 'task')['state'] == 'reconciled'
        assert finalize(store, request, operation_key(call)) == receipt
    else:
        with pytest.raises(HarnessError) as exc:
            reconcile_word_call(store, 'task')
        assert exc.value.code == 'state_corrupt'
        assert pending_result(store, call) is None
    assert dict(store.operation(receipt['receipt_id'])) == before


def test_plain_missing_style_record_cannot_recover_review_prepare(store, monkeypatch):
    from localoffice.word.contracts import PrepareCreation
    from localoffice.word.creation_repair import capture
    request = proposal(False)
    call = record_call(store, 'task', request)
    payload = PrepareCreation.model_validate(request).model_dump()
    payload['tool'] = 'word.create'
    assert capture(store, 'task', payload)['status'] == 'missing_styles'
    original = meta(store)
    no_execution(monkeypatch)
    assert reconcile_word_call(store, 'task')['state'] == 'unresolved'
    assert pending_result(store, call) is None
    assert meta(store) == original


def test_cross_chat_prepare_recovery_fails_before_consuming_call(store, monkeypatch):
    request = proposal()
    call = record_call(store, 'task', request)
    prepare(store, request, native_call_id=call)
    original = meta(store)
    no_execution(monkeypatch)
    monkeypatch.setattr('localoffice.native_history.current_chat', lambda _: 'another-chat')
    with pytest.raises(HarnessError) as exc:
        reconcile_word_call(store, 'task')
    assert exc.value.code == 'chat_scope'
    assert pending_result(store, call) is None
    assert meta(store) == original


def test_identical_unexecuted_prepare_cannot_borrow_prior_receipt(store):
    from localoffice.native_history import record_result
    request=proposal()
    first_call=record_call(store,'task',request)
    first=prepare(store,request,native_call_id=first_call)
    record_result(store,'task',first_call,first)
    second_call=record_call(store,'task',request)
    assert reconcile_word_call(store,'task')['state']=='unresolved'
    assert pending_result(store,second_call) is None
    second=prepare(store,request,native_call_id=second_call)
    assert second['ref']==first['ref']
    recovered=reconcile_word_call(store,'task')
    assert recovered['state']=='reconciled' and recovered['routing_restored'] is True
    saved=json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(recovered['message_id'],)).fetchone()[0])
    assert saved==second


def test_legacy_equal_payload_without_call_association_stays_unresolved(store):
    request=proposal();call=record_call(store,'task',request)
    prepare(store,request)
    assert reconcile_word_call(store,'task')['state']=='unresolved'
    assert pending_result(store,call) is None


def test_same_ref_with_unknown_submission_marker_recovers_history_without_routing(store):
    request=proposal();call=record_call(store,'task',request)
    saved=prepare(store,request,native_call_id=call)
    again=prepare(store,request)  # Explicit direct call has unknown native provenance.
    assert again['ref']==saved['ref']
    recovered=reconcile_word_call(store,'task')
    assert recovered['state']=='reconciled' and recovered['routing_restored'] is False
    assert store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone() is None


def test_submission_association_tamper_rejected_before_recovery(store):
    from localoffice.word.creation_submission import PREFIX as SUBMISSIONS
    request=proposal();call=record_call(store,'task',request)
    prepare(store,request,native_call_id=call)
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(SUBMISSIONS+call,)).fetchone()
    envelope=json.loads(row[0]);envelope['state']['receipt']['document_changed']=True
    with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),SUBMISSIONS+call))
    with pytest.raises(HarnessError) as caught:reconcile_word_call(store,'task')
    assert caught.value.code=='state_corrupt' and pending_result(store,call) is None
