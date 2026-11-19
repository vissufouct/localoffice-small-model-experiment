"""Historical creation identity compatibility; no model, build or rendering."""
import copy
import json
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.native_history import record_call, operation_key, reconcile_word_call
from localoffice.word.plugin import WordPlugin
from localoffice.word.creation_identity import VARIANT_FIELDS, normalize_creation_identity
from localoffice.word.content_plan import plan
from test_word_progress import task
from test_creation_evidence import package, para


@pytest.fixture
def historical(tmp_path, monkeypatch):
    store=ProjectStore(tmp_path);task(store)
    plugin=WordPlugin(store)
    data=package(para('Body'))
    calls=[]
    def execute(request, action, **kwargs):
        calls.append(action)
        return {'candidate_revision':digest(data)},data
    monkeypatch.setattr(plugin,'_execute',execute)
    def create(*, native=False, bound=False):
        arguments={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}]}
        if bound:
            with store.transaction():
                store.db.execute("UPDATE tasks SET objective='Body' WHERE id='task'")
            ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
            arguments['blocks'][0]['text']={'ref':ref}
        call=record_call(store,'task',{'tool':'word.create',**arguments}) if native else None
        key=operation_key(call) if call else 'legacy'
        receipt=plugin.create(**arguments,idempotency_key=key,task_id='task')
        row=store.db.execute('SELECT * FROM operations WHERE id=?',(receipt['receipt_id'],)).fetchone()
        saved=json.loads(row['args'])
        # Simulate a legitimately hashed receipt from before these four fields
        # existed, retaining every other historical default and binding fact.
        for name in ('request','submitted_request'):
            if name in saved:
                for field in VARIANT_FIELDS:
                    assert saved[name].pop(field) is None
        encoded=canonical(saved)
        with store.transaction():
            store.db.execute('UPDATE operations SET args=?,args_hash=? WHERE id=?',
                (encoded,digest(encoded.encode()),receipt['receipt_id']))
        return arguments,key,receipt,call
    yield store,plugin,calls,create
    store.close()


def ledger(store):
    return [dict(row) for row in store.db.execute('SELECT * FROM operations ORDER BY rowid')]


@pytest.mark.parametrize('bound',[False,True])
def test_legacy_receipt_replays_without_worker_or_ledger_rewrite(historical,bound):
    store,plugin,calls,create=historical
    args,key,receipt,_=create(bound=bound)
    before=ledger(store)
    assert plugin.create(**args,idempotency_key=key,task_id='task')==receipt
    assert plugin.create(**{**args,**dict.fromkeys(VARIANT_FIELDS)},idempotency_key=key,task_id='task')==receipt
    assert calls==['create'] and ledger(store)==before


@pytest.mark.parametrize('bound',[False,True])
def test_legacy_pending_native_call_recovers_exact_receipt(historical,bound):
    store,plugin,calls,create=historical
    args,key,receipt,call=create(native=True,bound=bound)
    before=ledger(store)
    result=reconcile_word_call(store,'task')
    assert result['state']=='reconciled' and result['receipt_id']==receipt['receipt_id']
    saved=store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(call,)).fetchone()[0]
    assert json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(saved,)).fetchone()[0])==receipt
    assert calls==['create'] and ledger(store)==before


@pytest.mark.parametrize('field',sorted(VARIANT_FIELDS))
@pytest.mark.parametrize('native',[False,True])
def test_nonnull_variant_still_conflicts(historical,field,native):
    store,plugin,calls,create=historical
    args,key,receipt,call=create(native=native)
    args[field]={'text':'New variant'}
    if native:
        with store.transaction():
            store.db.execute('UPDATE native_calls SET function=? WHERE call_id=?',
                (canonical({'name':'word.create','arguments':args}),call))
    before=ledger(store)
    with pytest.raises(HarnessError) as error:
        if native:reconcile_word_call(store,'task')
        else:plugin.create(**args,idempotency_key=key,task_id='task')
    assert error.value.code==('native_history' if native else 'idempotency_conflict')
    assert ledger(store)==before and calls==['create']


@pytest.mark.parametrize('native',[False,True])
def test_hash_tamper_not_hidden_by_null_normalization(historical,native):
    store,plugin,calls,create=historical
    args,key,receipt,call=create(native=native)
    row=ledger(store)[0];saved=json.loads(row['args'])
    saved['request']['first_header']=None  # Semantically normalized, but unhashed tampering.
    with store.transaction():
        store.db.execute('UPDATE operations SET args=? WHERE id=?',(canonical(saved),receipt['receipt_id']))
    before=ledger(store)
    with pytest.raises(HarnessError) as error:
        if native:reconcile_word_call(store,'task')
        else:plugin.create(**args,idempotency_key=key,task_id='task')
    assert error.value.code=='state_corrupt'
    assert ledger(store)==before and calls==['create']


def test_normalization_is_nonmutating_and_only_four_top_level_nulls():
    request={'header':None,'footer':None,'revision':None,'literal_refs':{},
             'blocks':[{'first_header':None}],**dict.fromkeys(VARIANT_FIELDS)}
    before=copy.deepcopy(request)
    assert normalize_creation_identity(request)=={k:v for k,v in request.items() if k not in VARIANT_FIELDS}
    assert request==before
    for value in ({},'',False,0,{'text':'Literal'}):
        for field in VARIANT_FIELDS:
            assert normalize_creation_identity({field:value})=={field:value}


@pytest.mark.parametrize('native',[False,True])
def test_other_default_changes_still_conflict(historical,native):
    store,plugin,calls,create=historical
    args,key,receipt,call=create(native=native)
    row=ledger(store)[0];saved=json.loads(row['args'])
    saved['request'].pop('header')
    encoded=canonical(saved)
    with store.transaction():
        store.db.execute('UPDATE operations SET args=?,args_hash=? WHERE id=?',
            (encoded,digest(encoded.encode()),receipt['receipt_id']))
    before=ledger(store)
    with pytest.raises(HarnessError) as error:
        if native:reconcile_word_call(store,'task')
        else:plugin.create(**args,idempotency_key=key,task_id='task')
    assert error.value.code==('native_history' if native else 'idempotency_conflict')
    assert ledger(store)==before and calls==['create']
