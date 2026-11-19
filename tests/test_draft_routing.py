"""Routing evidence only: no inference, document operations or automatic loads."""
import json

import pytest

from localoffice.chats import create_chat, current_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result, reconcile_word_call
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word import draft_fragments as drafts
from localoffice.word.draft_routing import should_return_to_document
from localoffice.word.guidance import load

OBJECTIVE = 'Keep exact text. Draft an invitation to review.'
TEXT = 'Please review this document. 😀'


@pytest.fixture
def store(tmp_path):
    instance = ProjectStore(tmp_path)
    with instance.transaction():
        instance.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                            ('task', OBJECTIVE, 'running', 'background', 'report.docx', 1, 1))
        instance.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(instance)))
    yield instance
    assert instance.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert not (tmp_path / 'report.docx').exists()
    instance.close()


def planned(store):
    return drafts.plan_draft(store, 'task', source_hash=digest(OBJECTIVE.encode()),
                             source_range={'start': 's02', 'end': 's02'})


def submitted(store, *, completed=True, ref=None):
    ref = ref or planned(store)['ref']
    request = {'tool': 'word.submit_draft', 'ref': ref, 'text': TEXT}
    call = record_call(store, 'task', request)
    receipt = drafts.submit_draft(store, 'task', ref=ref, text=TEXT, native_call_id=call)
    if completed:
        record_result(store, 'task', call, receipt)
    return call, receipt


def loaded(store, operations, *, failed=False):
    call = record_call(store, 'task', {'tool': 'word.load_tools', 'operations': operations})
    result = {'code': 'context_capacity', 'message': 'Not loaded.'} if failed else load(store, 'task', operations)
    record_result(store, 'task', call, result)
    return call


def checked(store, expected):
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    assert should_return_to_document(store, 'task') is expected
    assert store.db.total_changes == changes
    assert list(store.db.iterdump()) == before


def test_native_submission_returns_readonly(store):
    loaded(store, ['plan_draft'])
    submitted(store)
    checked(store, True)


def test_interrupted_submission_only_qualifies_after_exact_recovery(store):
    submitted(store, completed=False)
    checked(store, False)
    assert reconcile_word_call(store, 'task')['state'] == 'reconciled'
    checked(store, True)


@pytest.mark.parametrize('state', ['none', 'pending', 'direct', 'unexecuted', 'fake_result'])
def test_missing_native_execution_never_inferred(store, state):
    if state != 'none':
        ref = planned(store)['ref']
        if state not in ('pending',):
            receipt = drafts.submit_draft(store, 'task', ref=ref, text=TEXT)
            if state in ('unexecuted', 'fake_result'):
                call = record_call(store, 'task', {'tool': 'word.submit_draft', 'ref': ref, 'text': TEXT})
                if state == 'fake_result':
                    record_result(store, 'task', call, receipt)
    checked(store, False)


def test_source_stale_returns_false(store):
    submitted(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective='Changed source' WHERE id='task'")
    checked(store, False)


@pytest.mark.parametrize('new_state', ['pending', 'direct_drafted', 'native_drafted'])
def test_superseded_submission_never_authorizes_new_generation(store, new_state):
    old_call, old_receipt = submitted(store)
    fresh = planned(store)
    if new_state == 'direct_drafted':
        drafts.submit_draft(store, 'task', ref=fresh['ref'], text=TEXT)
    elif new_state == 'native_drafted':
        submitted(store, ref=fresh['ref'])
    # Replaying exact old history must not restore the historical generation.
    assert drafts.submit_draft(store, 'task', ref=old_receipt['ref'], text=TEXT,
                               native_call_id=old_call) == old_receipt
    checked(store, new_state == 'native_drafted')


@pytest.mark.parametrize('operations', [['plan_draft'], ['inspect'], ['prepare_creation'],
                                       ['plan_draft', 'inspect']])
def test_newer_successful_explicit_load_is_preserved(store, operations):
    submitted(store)
    loaded(store, operations)
    checked(store, False)


def test_newer_identical_load_and_identical_submission_retry_cannot_erase_choice(store):
    loaded(store, ['plan_draft'])
    _, receipt = submitted(store)
    loaded(store, ['plan_draft'])
    submitted(store, ref=receipt['ref'])
    checked(store, False)


@pytest.mark.parametrize('operations', [['plan_draft'], ['prepare_creation']])
def test_failed_load_does_not_block_return(store, operations):
    submitted(store)
    loaded(store, operations, failed=True)
    checked(store, True)


def test_pending_new_load_is_not_overridden(store):
    submitted(store)
    record_call(store, 'task', {'tool': 'word.load_tools', 'operations': ['plan_draft']})
    checked(store, False)


@pytest.mark.parametrize('tamper', ['receipt', 'result_text', 'call_text', 'assistant', 'result_chat',
                                  'call_chat', 'generation', 'load_result'])
def test_tampered_evidence_fails_closed(store, tamper):
    call, receipt = submitted(store)
    row = store.db.execute('SELECT * FROM native_calls WHERE call_id=?', (call,)).fetchone()
    if tamper == 'load_result':
        call = loaded(store, ['inspect'])
        row = store.db.execute('SELECT * FROM native_calls WHERE call_id=?', (call,)).fetchone()
    with store.transaction():
        if tamper == 'receipt':
            store.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', drafts.RESULT_PREFIX + call))
        elif tamper in ('result_text', 'generation'):
            changed = {**receipt, 'generation': receipt['generation'] + 1}
            if tamper == 'result_text':changed['status'] = 'pending'
            store.db.execute('UPDATE messages SET content=? WHERE id=?', (canonical(changed), row['result_message']))
        elif tamper == 'call_text':
            function = json.loads(row['function'])
            function['arguments']['text'] = 'Competing text'
            store.db.execute('UPDATE native_calls SET function=? WHERE call_id=?', (canonical(function), call))
            store.db.execute('UPDATE messages SET content=? WHERE id=?',
                             (canonical({'tool': function['name'], **function['arguments']}), row['assistant_message']))
        elif tamper == 'assistant':
            store.db.execute("UPDATE messages SET content='{}' WHERE id=?", (row['assistant_message'],))
        elif tamper == 'result_chat':
            store.db.execute("UPDATE message_chats SET chat_id='other' WHERE message_id=?", (row['result_message'],))
        elif tamper == 'call_chat':
            store.db.execute("UPDATE native_calls SET chat_id='other' WHERE call_id=?", (call,))
        elif tamper == 'load_result':
            store.db.execute('UPDATE messages SET content=? WHERE id=?',
                             (canonical({'loaded_operations': ['plan_draft']}), row['result_message']))
    before = list(store.db.iterdump())
    with pytest.raises(HarnessError):should_return_to_document(store, 'task')
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize('change', ['chat', 'target'])
def test_current_owner_scope_failure_propagates(store, change):
    submitted(store)
    if change == 'chat':
        select_chat(store, create_chat(store, 'Other chat'))
    else:
        with store.transaction():
            store.db.execute("UPDATE tasks SET artifact='different.docx' WHERE id='task'")
    with pytest.raises(HarnessError):should_return_to_document(store, 'task')


def test_other_tasks_load_does_not_block_owned_return(store):
    submitted(store)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('other', OBJECTIVE, 'running', 'background', 'other.docx', 1, 1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)', ('other', current_chat(store)))
    call = record_call(store, 'other', {'tool': 'word.load_tools', 'operations': ['plan_draft']})
    record_result(store, 'other', call, load(store, 'other', ['plan_draft']))
    checked(store, True)
