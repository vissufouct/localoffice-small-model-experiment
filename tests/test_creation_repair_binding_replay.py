"""Explicit repair bindings retain original design and exact durable call identity."""
import io
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest

from localoffice.errors import HarnessError
from localoffice.native_history import operation_key, record_call, reconcile_word_call
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word.content_plan import active_plan, plan
from localoffice.word.creation_repair import capture
from localoffice.word.creation_revision import revise
from localoffice.word.plugin import WordPlugin
from test_creation_repair_runtime import REQUEST, STYLES
from test_word_creation import W
from test_word_progress import task


@pytest.fixture
def proposal(tmp_path):
    store = ProjectStore(tmp_path)
    task(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective=? WHERE id='task'",
                         ('Exact body; Exact header; Page ; Sheet ',))
    refs = plan(store, 'task', literals=[{'role': 'footer', 'quote': 'Page '},
                                       {'role': 'footer', 'quote': 'Sheet '}])['literals']
    request = json.loads(json.dumps(REQUEST))
    request['footer'].update(text='Page of', separator=' of ', alignment='center')
    hint = capture(store, 'task', request)
    assert hint is not None
    try:
        yield store, request, hint['ref'], refs[0]['ref'], refs[1]['ref']
    finally:
        store.close()


def action(ref, bindings=None):
    result = {'tool': 'word.repair_creation', 'ref': ref, 'styles': STYLES}
    if bindings is not None:
        result['source_bindings'] = bindings
    return result


def execute(store, request, key):
    return WordPlugin(store).repair_creation(
        **{k: v for k, v in request.items() if k != 'tool'},
        task_id='task', idempotency_key=key)


def ledger(store, receipt):
    return dict(store.operation(receipt['receipt_id']))


def footer_xml(store, receipt):
    with zipfile.ZipFile(io.BytesIO(store.blob(receipt['candidate_revision']))) as package:
        roots = [ET.fromstring(package.read(n)) for n in package.namelist()
                 if n.startswith('word/') and n.endswith('.xml')]
    return next(r for r in roots if r.tag == '{' + W + '}ftr')


@pytest.mark.parametrize('explicit', [False, True])
def test_repair_binding_changes_only_explicit_footer_and_retains_provenance(proposal, explicit):
    store, original, ref, footer_ref, _ = proposal
    if explicit:
        with pytest.raises(HarnessError) as caught:
            execute(store, action(ref, {'footer.text': footer_ref}), 'conflict')
        assert caught.value.code=='content_conflict'
        ref=revise(store,'task',ref,[{'op':'set','path':'footer.text','value':'Page '}])['ref']
        original=json.loads(json.dumps(original));original['footer']['text']='Page '
    bindings = {'footer.text': footer_ref} if explicit else None
    receipt = execute(store, action(ref, bindings), 'repair')
    saved = json.loads(ledger(store, receipt)['args'])
    expected = 'Page ' if explicit else 'Page of'
    assert saved['repair_origin']['payload'] == original
    assert saved['repair_origin']['payload_hash'] == digest(canonical(original).encode())
    assert saved['request']['footer']['text'] == expected
    assert saved['request']['blocks'][0]['text'] == original['blocks'][0]['text']
    assert saved['request']['header']['text'] == original['header']['text']
    for key in ('style', 'separator', 'alignment', 'page_numbers'):
        assert saved['request']['footer'][key] == original['footer'][key]
    assert saved['repair_request'].get('source_bindings', {}) == (bindings or {})
    if explicit:
        provenance = json.loads(store.db.execute(
            'SELECT body FROM word_content_refs WHERE ref=? AND task_id=?',
            (footer_ref, 'task')).fetchone()[0])
        assert saved['literal_bindings'] == [{**provenance, 'slot': 'footer.text'}]
    else:
        assert not saved.get('literal_bindings')
    footer = footer_xml(store, receipt)
    assert footer.find('.//{' + W + '}t').text == expected
    assert [f.get('{' + W + '}instr').strip() for f in footer.findall('.//{' + W + '}fldSimple')] == ['PAGE', 'NUMPAGES']
    assert footer.find('.//{' + W + '}jc').get('{' + W + '}val') == 'center'
    assert not (store.root / 'report.docx').exists()


def test_exact_binding_replay_after_plan_replacement_and_restart_never_resolves_or_executes(proposal, monkeypatch):
    store, _, ref, footer_ref, alternate = proposal
    ref=revise(store,'task',ref,[{'op':'set','path':'footer.text','value':'Page '}])['ref']
    request = action(ref, {'footer.text': footer_ref})
    call = record_call(store, 'task', request)
    receipt = execute(store, request, operation_key(call))
    before = ledger(store, receipt)
    plan(store, 'task', plan_mode='replace', literals=[{'role': 'footer', 'quote': 'Sheet '}])
    assert active_plan(store, 'task')['refs'] == [{'ref': alternate, 'role': 'footer'}]
    root = store.root
    store.close()
    reopened = ProjectStore(root)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail('Exact ledger replay must not resolve sources or execute a worker')
        monkeypatch.setattr(WordPlugin, '_execute', forbidden)
        monkeypatch.setattr('localoffice.word.creation_repair.resolve', forbidden)
        assert reconcile_word_call(reopened, 'task')['state'] == 'reconciled'
        assert execute(reopened, request, operation_key(call)) == receipt
        assert ledger(reopened, receipt) == before
        with pytest.raises(HarnessError) as exc:
            execute(reopened, action(ref, {'footer.text': alternate}), operation_key(call))
        assert exc.value.code == 'idempotency_conflict'
        assert ledger(reopened, receipt) == before
        assert reopened.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('native_map', [None, {}])
def test_historical_missing_map_legitimate_hash_replays_and_recovers_without_ledger_rewrite(proposal, monkeypatch, native_map):
    store, _, ref, _, _ = proposal
    request = action(ref, native_map)
    call = record_call(store, 'task', request)
    receipt = execute(store, request, operation_key(call))
    saved = json.loads(ledger(store, receipt)['args'])
    saved['repair_request'].pop('source_bindings', None)
    encoded = canonical(saved)
    with store.transaction():
        store.db.execute('UPDATE operations SET args=?,args_hash=? WHERE id=?',
                         (encoded, digest(encoded.encode()), receipt['receipt_id']))
    before = ledger(store, receipt)
    def forbidden(*args, **kwargs):
        pytest.fail('Historical receipt recovery must use the exact ledger')
    monkeypatch.setattr(WordPlugin, '_execute', forbidden)
    monkeypatch.setattr('localoffice.word.creation_repair.resolve', forbidden)
    assert execute(store, action(ref, {}), operation_key(call)) == receipt
    assert reconcile_word_call(store, 'task')['state'] == 'reconciled'
    assert ledger(store, receipt) == before
    assert 'source_bindings' not in json.loads(before['args'])['repair_request']


@pytest.mark.parametrize('recovery', [False, True])
def test_missing_empty_normalization_never_hides_original_hash_tampering(proposal, recovery):
    store, _, ref, _, _ = proposal
    request = action(ref, {})
    call = record_call(store, 'task', request)
    receipt = execute(store, request, operation_key(call))
    saved = json.loads(ledger(store, receipt)['args'])
    assert saved['repair_request']['source_bindings'] == {}
    del saved['repair_request']['source_bindings']
    with store.transaction():
        store.db.execute('UPDATE operations SET args=? WHERE id=?',
                         (canonical(saved), receipt['receipt_id']))
    before = ledger(store, receipt)
    with pytest.raises(HarnessError) as exc:
        if recovery:
            reconcile_word_call(store, 'task')
        else:
            execute(store, request, operation_key(call))
    assert exc.value.code == 'state_corrupt'
    assert ledger(store, receipt) == before
    assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?', (call,)).fetchone()[0] is None
