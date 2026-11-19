"""Read-only complete explicit style evidence, never implicit Normal inference."""
import copy

import pytest

from localoffice.chats import create_chat, current_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore
from localoffice.word import creation_repair as repair
from localoffice.word.creation_revision import revise
from localoffice.word.style_inventory import explicit_style_inventory


@pytest.fixture
def store(tmp_path):
    instance = ProjectStore(tmp_path)
    with instance.transaction():
        instance.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                            ('task', 'Create a styled document.', 'running', 'background', 'report.docx', 1, 1))
        instance.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(instance)))
    yield instance
    assert instance.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    instance.close()


def retain(store, styles=None):
    payload = {'path': 'report.docx', 'blocks': [{'kind': 'paragraph', 'text': 'Exact café 😀'}]}
    if styles is not None:payload['styles'] = copy.deepcopy(styles)
    return repair.capture(store, 'task', payload, allow_valid=True)['ref']


def read(store, ref):
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    result = explicit_style_inventory(store, 'task', ref)
    assert list(store.db.iterdump()) == before and store.db.total_changes == changes
    return result


@pytest.mark.parametrize('styles', [None, []])
def test_omitted_or_empty_explicit_styles_verified_empty_despite_implicit_normal(store, styles):
    ref = retain(store, styles)
    state = repair.get(store, 'task')
    assert state['declared'] == ['Normal']
    assert read(store, ref) == {'version': 1, 'ref': ref, 'payload_hash': state['payload_hash'],
                               'status': 'verified', 'complete': True, 'count': 0, 'ids': []}


def test_explicit_normal_and_case_sensitive_ids_preserve_declaration_order(store):
    styles = [{'id': 'Zebra', 'font': 'Georgia', 'size_half_points': 25},
              {'id': 'Normal', 'font': 'Arial', 'size_points': 12, 'bold': False},
              {'id': 'body'}, {'id': 'Body', 'color': 'aAbBcC', 'heading_level': 2}]
    ref = retain(store, styles)
    inventory = read(store, ref)
    assert inventory['ids'] == ['Zebra', 'Normal', 'body', 'Body']
    assert inventory['count'] == 4 and inventory['complete'] is True
    inventory['ids'].clear()
    assert read(store, ref)['count'] == 4
    assert repair.get(store, 'task')['payload']['styles'] == styles


def test_full_sixteen_styles_not_truncated_by_proposal_summary(store):
    styles = [{'id': 'Style' + str(i), 'font': 'Font' + 'x' * 50} for i in range(16)]
    ref = retain(store, styles)
    assert repair.pending(store, 'task')['proposed_design']['omitted'] is True
    result = read(store, ref)
    assert result['status'] == 'verified' and result['count'] == 16
    assert result['ids'] == [style['id'] for style in styles]


def test_missing_and_superseded_are_unavailable_not_empty(store):
    missing = read(store, 'cr_' + 'a' * 32)
    assert missing['status'] == 'unavailable' and missing['reason'] == 'missing'
    assert missing['complete'] is False and missing['ids'] is None and missing['count'] is None
    old = retain(store)
    new = retain(store, [{'id': 'Body'}])
    historical = read(store, old)
    assert historical['status'] == 'unavailable' and historical['reason'] == 'superseded'
    assert historical['ids'] is None and historical['payload_hash'] is None
    assert read(store, new)['ids'] == ['Body']


@pytest.mark.parametrize('change', ['objective', 'source', 'target', 'disk', 'candidate'])
def test_stale_current_proposal_never_yields_verified_ids(store, change):
    ref = retain(store, [{'id': 'Body'}])
    if change == 'source':store.message('user', 'A new instruction.', 'task')
    elif change == 'disk':(store.root / 'report.docx').write_bytes(b'Existing target')
    else:
        with store.transaction():
            if change == 'objective':store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
            elif change == 'target':store.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
            elif change == 'candidate':store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)', ('report.docx', 'op', None, 'a' * 64))
    result = read(store, ref)
    assert result['status'] == 'unavailable' and result['reason'] == 'stale'
    assert result['complete'] is False and result['ids'] is None


def test_duplicate_ids_are_explicitly_ambiguous(store):
    ref = retain(store, [{'id': 'Body', 'font': 'Arial'}, {'id': 'Body', 'font': 'Georgia'}])
    result = read(store, ref)
    assert result['status'] == 'unavailable' and result['reason'] == 'ambiguous'
    assert result['ids'] is None and result['count'] is None


def test_revision_inventory_uses_current_payload_without_submission_presence(store):
    ref = retain(store)
    changed = revise(store, 'task', ref, [{'op': 'set', 'path': 'styles', 'value': [{'id': 'Normal', 'size_points': 12}]}])
    state = repair.get(store, 'task')
    from localoffice.word.creation_submission import presence
    assert presence(store, state) is None
    assert read(store, ref)['reason'] == 'superseded'
    inventory = read(store, changed['ref'])
    assert inventory['ids'] == ['Normal'] and inventory['payload_hash'] == state['payload_hash']


@pytest.mark.parametrize('case', ['wrong_chat', 'missing_task', 'tamper'])
def test_scope_and_integrity_failures_propagate(store, case):
    ref = retain(store)
    task_id = 'task'
    if case == 'wrong_chat':select_chat(store, create_chat(store, 'Other chat'))
    elif case == 'missing_task':task_id = 'other'
    else:
        with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', repair.PREFIX + 'task'))
    before = list(store.db.iterdump())
    with pytest.raises(HarnessError):explicit_style_inventory(store, task_id, ref)
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize('styles', [None, {}, [{'id': 'bad id'}], [{'id': 'Body', 'font': ''}],
    [{'id': 'Body', 'size_points': 12, 'size_half_points': 24}], [{'id': 'Body', 'unknown': True}],
    [{'id': 'Body', 'bold': 'false'}], [{'id': 'S' + str(i)} for i in range(17)]])
def test_rehashed_malformed_styles_fail_closed(store, styles):
    ref = retain(store)
    state = repair.get(store, 'task')
    state['payload']['styles'] = styles
    state['payload_hash'] = repair._hash(state['payload'])
    with store.transaction():repair._save(store, state)
    before = list(store.db.iterdump())
    with pytest.raises(HarnessError) as exc:explicit_style_inventory(store, 'task', ref)
    assert exc.value.code == 'state_corrupt'
    assert list(store.db.iterdump()) == before
