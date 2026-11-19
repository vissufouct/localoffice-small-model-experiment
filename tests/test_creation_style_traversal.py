"""Style traversal diagnostics only; no inferred edits or model/worker calls."""
import copy

import pytest

from localoffice.chats import create_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.word import creation_repair as repair
from localoffice.word.creation_revision import revise
from test_word_content_plan import fixture


@pytest.fixture
def store(tmp_path):
    value = fixture(tmp_path, 'Preserve this proposal.')
    yield value
    value.close()


def retained(store, styles=None):
    payload = {'path': 'report.docx', 'blocks': [{'kind': 'paragraph', 'text': 'Keep exactly.'}]}
    if styles is not None: payload['styles'] = copy.deepcopy(styles)
    ref = repair.capture(store, 'task', payload, allow_valid=True)['ref']
    return ref, payload


def rejected(store, ref, changes):
    before = list(store.db.iterdump())
    original = copy.deepcopy(changes)
    with pytest.raises(HarnessError) as caught:
        revise(store, 'task', ref, changes)
    assert list(store.db.iterdump()) == before and changes == original
    return caught.value


@pytest.mark.parametrize('styles', [None, []])
@pytest.mark.parametrize('path', ['styles.0.font', 'styles.0.size_points', 'styles.0'])
def test_empty_or_omitted_styles_precise_feedback_no_changes(store, styles, path):
    ref, _ = retained(store, styles)
    error = rejected(store, ref, [{'op': 'set', 'path': path, 'value': 'Requested'}])
    text = str(error)
    assert error.code == 'creation_revision' and text.startswith(path + ': 0 explicit styles.')
    assert 'Implicit Normal is not indexed.' in text
    assert 'Batch rejected; no changes applied.' in text and 'batch-intermediate' not in text
    if styles is None:
        assert 'styles is absent: set styles declares the list' in text
        assert 'insert requires that list' in text
    else:
        assert 'set styles replaces the list; insert styles.0 appends a declaration' in text
    assert 'Arial' not in text and 'size_points=12' not in text


def test_nonempty_count_does_not_call_implicit_normal_an_index(store):
    ref, _ = retained(store, [{'id': 'Body', 'font': 'Georgia', 'size_points': 10}])
    text = str(rejected(store, ref, [{'op': 'set', 'path': 'styles.1.font', 'value': 'Requested'}]))
    assert '1 explicit styles.' in text and 'insert styles.1 appends' in text
    assert 'Implicit Normal' not in text and 'Preserve existing entries.' in text


@pytest.mark.parametrize('prior', [
    {'op': 'insert', 'path': 'styles.0', 'value': {'id': 'Body', 'font': 'Georgia'}},
    {'op': 'set', 'path': 'styles', 'value': [{'id': 'Body', 'font': 'Georgia'}]},
])
def test_intermediate_count_is_labelled_and_entire_batch_rolls_back(store, prior):
    ref, _ = retained(store, [])
    text = str(rejected(store, ref, [prior, {'op': 'set', 'path': 'styles.1.font', 'value': 'Requested'}]))
    assert '1 explicit styles at failing change (batch-intermediate)' in text
    assert 'insert styles.1 appends' in text
    assert repair.get(store, 'task')['payload']['styles'] == []


def test_intermediate_removed_list_is_not_treated_as_appendable(store):
    ref, _ = retained(store, [{'id': 'Body'}])
    text = str(rejected(store, ref, [{'op': 'remove', 'path': 'styles'},
                                   {'op': 'set', 'path': 'styles.0.font', 'value': 'Requested'}]))
    assert '0 explicit styles at failing change (batch-intermediate)' in text
    assert 'styles is absent' in text and 'insert requires that list' in text


@pytest.mark.parametrize('mode', ['replace', 'append', 'existing'])
def test_explicit_caller_declarations_and_indexed_edits_still_work(store, mode):
    original = [{'id': 'Body', 'font': 'Georgia', 'size_points': 10}]
    ref, payload = retained(store, original if mode != 'replace' else [])
    declaration = {'id': 'Other', 'font': 'Courier New', 'size_points': 13}
    if mode == 'replace':
        changes = [{'op': 'set', 'path': 'styles', 'value': [declaration]}]
        expected = [declaration]
    elif mode == 'append':
        changes = [{'op': 'insert', 'path': 'styles.1', 'value': declaration}]
        expected = original + [declaration]
    else:
        changes = [{'op': 'set', 'path': 'styles.0.size_points', 'value': 13}]
        expected = [{**original[0], 'size_points': 13}]
    result = revise(store, 'task', ref, changes)
    actual = repair.get(store, 'task')['payload']
    assert actual['styles'] == expected and actual['blocks'] == payload['blocks']
    assert result['ref'] != ref and store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0


@pytest.mark.parametrize('case', ['stale', 'superseded', 'wrong_chat', 'corrupt', 'syntax', 'non_style'])
def test_existing_guards_take_precedence(store, case):
    ref, _ = retained(store, [])
    change = {'op': 'set', 'path': 'styles.0.font', 'value': 'Requested'}
    if case == 'stale':
        with store.transaction(): store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    elif case == 'superseded': revise(store, 'task', ref, [{'op': 'set', 'path': 'blocks.0.text', 'value': 'Changed'}])
    elif case == 'wrong_chat': select_chat(store, create_chat(store))
    elif case == 'corrupt':
        with store.transaction(): store.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', repair.PREFIX + 'task'))
    elif case == 'syntax': change['path'] = 'styles.01.font'
    else: change['path'] = 'blocks.8.text'
    error = rejected(store, ref, [change])
    assert 'explicit styles' not in str(error) and 'Implicit Normal' not in str(error)
    if case == 'non_style': assert str(error) == 'Proposal array index is out of range.'
