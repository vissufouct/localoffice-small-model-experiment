"""Offline typed style contracts, exact receipts and proposal preservation."""
import copy
import json

import pytest
from pydantic import ValidationError

from localoffice.chats import create_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.storage import canonical, digest
from localoffice.word import creation_repair as repair, creation_styles as styles
from localoffice.word.creation_revision import revise
from localoffice.word.contracts import EditCreationStyles, LoadTools
from test_word_content_plan import fixture

REF = 'cr_' + 'a' * 32


@pytest.fixture
def ctx(tmp_path):
    store = fixture(tmp_path, 'Keep all content and layout.')
    yield store
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert store.candidate('report.docx') is None
    store.close()


def retained(store, explicit=None):
    payload = {'path': 'report.docx', 'page': {'margin_twips': 1440},
               'blocks': [{'kind': 'paragraph', 'text': 'Keep café 😀 exactly.', 'style': 'Normal'},
                          {'kind': 'paragraph', 'text': 'Ending', 'page_break_before': True}]}
    if explicit is not None: payload['styles'] = copy.deepcopy(explicit)
    return repair.capture(store, 'task', payload, allow_valid=True)['ref'], payload


def declaration(style_id='Normal'):
    return {'op': 'declare', 'id': style_id, 'font': 'Georgia', 'size_points': 12}


def unchanged_failure(store, operation, exception=(HarnessError, ValidationError)):
    before = list(store.db.iterdump())
    with pytest.raises(exception): operation()
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize('explicit', [None, []])
def test_declare_implicit_normal_without_assigning_paragraphs(ctx, explicit):
    ref, original = retained(ctx, explicit)
    change = declaration()
    result = styles.edit(ctx, 'task', ref=ref, changes=[change])
    expected = copy.deepcopy(original)
    expected['styles'] = [{'id': 'Normal', 'font': 'Georgia', 'size_points': 12.0}]
    assert repair.get(ctx, 'task')['payload'] == expected
    assert change == declaration() and result['ref'] != ref
    assert styles.edit(ctx, 'task', ref=ref, changes=[change]) == result


def test_sparse_update_preserves_all_other_style_and_document_fields(ctx):
    explicit = [{'id': 'Body', 'font': 'Georgia', 'size_half_points': 22, 'bold': True,
                 'italic': True, 'color': '123456', 'alignment': 'center', 'spacing_after_twips': 260},
                {'id': 'Other', 'font': 'Courier New', 'size_points': 9}]
    ref, original = retained(ctx, explicit)
    changes = [{'op': 'update', 'id': 'Body', 'bold': False, 'size_points': 13}]
    styles.edit(ctx, 'task', ref=ref, changes=changes)
    expected = copy.deepcopy(original)
    expected['styles'][0].pop('size_half_points')
    expected['styles'][0].update(bold=False, size_points=13.0)
    assert repair.get(ctx, 'task')['payload'] == expected
    assert styles.normalize({'ref': ref, 'changes': changes})['changes'] == changes


def test_update_without_size_preserves_legacy_size(ctx):
    ref, _ = retained(ctx, [{'id': 'Normal', 'size_half_points': 24}])
    styles.edit(ctx, 'task', ref=ref, changes=[{'op': 'update', 'id': 'Normal', 'italic': False}])
    assert repair.get(ctx, 'task')['payload']['styles'] == [{'id': 'Normal', 'size_half_points': 24, 'italic': False}]


def test_mixed_batch_uses_original_ids_and_preserves_order(ctx):
    ref, _ = retained(ctx, [{'id': 'Body', 'font': 'Arial', 'size_points': 11}, {'id': 'body'}])
    styles.edit(ctx, 'task', ref=ref, changes=[declaration('New'), {'op': 'update', 'id': 'Body', 'font': 'Georgia'},
                                             declaration('Last'), {'op': 'update', 'id': 'body', 'color': 'abcdef'}])
    actual = repair.get(ctx, 'task')['payload']['styles']
    assert [s['id'] for s in actual] == ['Body', 'body', 'New', 'Last']
    assert actual[0] == {'id': 'Body', 'font': 'Georgia', 'size_points': 11}
    assert actual[1] == {'id': 'body', 'color': 'abcdef'}


@pytest.mark.parametrize('change', [
    {'op': 'declare', 'id': 'New', 'font': 'Arial'},
    {'op': 'declare', 'id': 'New', 'size_points': 12},
    {'op': 'update', 'id': 'Normal'},
    {'op': 'update', 'id': 'Normal', 'font': None},
    {'op': 'update', 'id': 'Normal', 'bold': None},
    {'op': 'update', 'id': 'Normal', 'bold': 0},
    {'op': 'update', 'id': 'Normal', 'italic': 'false'},
    {'op': 'update', 'id': 'Normal', 'size_points': True},
    {'op': 'update', 'id': 'Normal', 'size_points': '12'},
    {'op': 'update', 'id': 'Normal', 'size_points': 5},
    {'op': 'update', 'id': 'Normal', 'size_points': 145},
    {'op': 'update', 'id': 'Normal', 'size_points': 12.25},
    {'op': 'update', 'id': 'Normal', 'size_points': float('nan')},
    {'op': 'update', 'id': 'Normal', 'size_points': float('inf')},
    {'op': 'update', 'id': 'Normal', 'font': ''},
    {'op': 'update', 'id': 'Normal', 'font': 'x' * 81},
    {'op': 'update', 'id': 'Normal', 'color': '#123456'},
    {'op': 'update', 'id': 'Normal', 'color': 'red'},
    {'op': 'update', 'id': 'Normal', 'size_half_points': 24},
    {'op': 'update', 'id': 'Normal', 'spacing_after_twips': 100},
    {'op': 'update', 'id': 'Normal', 'path': 'styles.0', 'font': 'Arial'},
    {'op': 'update', 'id': 'bad id', 'font': 'Arial'},
    {'op': 'upsert', 'id': 'Normal', 'font': 'Arial'},
    {**declaration(), 'color': None},
])
@pytest.mark.parametrize('transport', ['python', 'json'])
def test_strict_sparse_contract(change, transport):
    request = {'ref': REF, 'changes': [change]}
    with pytest.raises(ValidationError):
        if transport == 'json': EditCreationStyles.model_validate_json(json.dumps(request))
        else: EditCreationStyles.model_validate(request)


@pytest.mark.parametrize('changes', [[], [declaration(str(i)) for i in range(5)],
    [declaration(), declaration()], [declaration(), {'op': 'update', 'id': 'Normal', 'bold': False}],
    [{'op': 'update', 'id': 'Body', 'bold': False}, {'op': 'update', 'id': 'Body', 'italic': True}]])
def test_batch_bounds_and_duplicate_ids(changes):
    with pytest.raises(ValidationError): EditCreationStyles(ref=REF, changes=changes)


def test_roundtrip_false_omission_and_load_discovery():
    args = {'ref': REF, 'changes': [declaration(), {'op': 'update', 'id': 'Body', 'bold': False}]}
    normalized = styles.normalize(args)
    assert styles.normalize(json.loads(EditCreationStyles(**args).model_dump_json())) == normalized
    assert normalized['changes'][1] == args['changes'][1]
    assert normalized['changes'][0] == declaration()
    assert LoadTools(operations=['edit_creation_styles', 'revise_creation']).operations == ['edit_creation_styles', 'revise_creation']


@pytest.mark.parametrize('case', ['existing_declare', 'implicit_update', 'unknown_update', 'case_mismatch'])
def test_explicit_declare_update_guards_roll_back(ctx, case):
    ref, _ = retained(ctx, [{'id': 'Body', 'font': 'Arial'}])
    change = declaration('Body') if case == 'existing_declare' else {
        'op': 'update', 'id': {'implicit_update': 'Normal', 'unknown_update': 'Missing', 'case_mismatch': 'body'}[case], 'bold': False}
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', ref=ref, changes=[declaration('New'), change]))


def test_maximum_sixteen_explicit_styles_and_updates_at_limit(ctx):
    ref, _ = retained(ctx, [{'id': 'S' + str(i)} for i in range(15)])
    result = styles.edit(ctx, 'task', ref=ref, changes=[declaration('Last')])
    assert len(repair.get(ctx, 'task')['payload']['styles']) == 16
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', ref=result['ref'], changes=[declaration('Overflow')]))
    styles.edit(ctx, 'task', ref=result['ref'], changes=[{'op': 'update', 'id': 'S0', 'bold': False}])


def test_sparse_noop_and_generic_supersession(ctx):
    ref, _ = retained(ctx, [{'id': 'Body', 'font': 'Arial'}])
    change = [{'op': 'update', 'id': 'Body', 'font': 'Arial'}]
    first = styles.edit(ctx, 'task', ref=ref, changes=change)
    assert first['ref'] != ref and first['edit_effect']['proposal_changed'] is False
    assert styles.edit(ctx, 'task', ref=ref, changes=change) == first
    revise(ctx, 'task', first['ref'], [{'op': 'set', 'path': 'styles.0.font', 'value': 'Arial'}])
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', ref=ref, changes=change))


@pytest.mark.parametrize('case', ['stale', 'wrong_chat', 'wrong_target', 'existing_target', 'corrupt'])
def test_proposal_guards_precede_edits(ctx, case):
    ref, _ = retained(ctx)
    if case == 'wrong_chat': select_chat(ctx, create_chat(ctx))
    elif case == 'existing_target': (ctx.root / 'report.docx').write_bytes(b'existing')
    else:
        with ctx.transaction():
            if case == 'stale': ctx.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
            elif case == 'wrong_target': ctx.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
            else: ctx.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', repair.PREFIX + 'task'))
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', ref=ref, changes=[declaration()]))


def native_edit(store):
    ref, _ = retained(store)
    request = {'ref': ref, 'changes': [declaration()]}
    call = record_call(store, 'task', {'tool': 'word.edit_creation_styles', **request})
    receipt = styles.edit(store, 'task', **request, native_call_id=call)
    return request, call, receipt


def test_native_occurrences_historical_receipts_and_replay(ctx):
    request, first, receipt = native_edit(ctx)
    assert styles.result(ctx, 'task', request, native_call_id=first) == receipt
    record_result(ctx, 'task', first, receipt)
    second = record_call(ctx, 'task', {'tool': 'word.edit_creation_styles', **request})
    assert styles.result(ctx, 'task', request, native_call_id=second) is None
    assert styles.edit(ctx, 'task', **request, native_call_id=second) == receipt
    record_result(ctx, 'task', second, receipt)
    with ctx.transaction(): ctx.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    before = list(ctx.db.iterdump())
    assert styles.result(ctx, 'task', request, native_call_id=first) == receipt
    assert styles.edit(ctx, 'task', **request, native_call_id=first) == receipt
    assert list(ctx.db.iterdump()) == before
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', **request))


def test_wrong_native_request_and_completed_unexecuted_call(ctx):
    ref, _ = retained(ctx)
    request = {'ref': ref, 'changes': [declaration()]}
    call = record_call(ctx, 'task', {'tool': 'word.edit_creation_styles', **request})
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', ref=ref, changes=[declaration('Other')], native_call_id=call))
    record_result(ctx, 'task', call, {'status': 'not_executed'})
    assert styles.result(ctx, 'task', request, native_call_id=call) is None
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', **request, native_call_id=call))


@pytest.mark.parametrize('part', ['envelope', 'request', 'receipt'])
def test_native_receipt_tamper_rejected(ctx, part):
    request, call, _ = native_edit(ctx)
    key = styles.PREFIX + call
    envelope = json.loads(ctx.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
    if part == 'request': envelope['state']['request']['changes'][0]['font'] = 'Changed'
    else: envelope['state']['receipt']['status'] = 'invented'
    if part != 'envelope': envelope['hash'] = digest(canonical(envelope['state']).encode())
    with ctx.transaction(): ctx.db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(envelope), key))
    unchanged_failure(ctx, lambda: styles.result(ctx, 'task', request, native_call_id=call))


def test_receipt_failure_rolls_back_proposal_and_replay_metadata(ctx):
    ref, _ = retained(ctx)
    request = {'ref': ref, 'changes': [declaration()]}
    call = record_call(ctx, 'task', {'tool': 'word.edit_creation_styles', **request})
    with ctx.transaction():
        ctx.db.execute("CREATE TRIGGER reject_style_receipt BEFORE INSERT ON meta WHEN NEW.key LIKE 'word_creation_style_result:%' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    unchanged_failure(ctx, lambda: styles.edit(ctx, 'task', **request, native_call_id=call), Exception)


def test_native_receipt_wrong_owner(ctx):
    request, call, _ = native_edit(ctx)
    select_chat(ctx, create_chat(ctx))
    unchanged_failure(ctx, lambda: styles.result(ctx, 'task', request, native_call_id=call))
