"""Native recovery preserves typed call identity; no worker or model runs."""
import copy
import json

import pytest

from localoffice.errors import HarnessError
from localoffice.native_history import exchanges, operation_key, record_call, reconcile_word_call
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word.content_plan import plan
from localoffice.word.creation_repair import capture
from localoffice.word.plugin import WordPlugin
from test_creation_evidence import package, para
from test_word_progress import task


@pytest.fixture(params=['word.repair_creation', 'word.finalize_creation'])
def context(tmp_path, monkeypatch, request):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective='Exact header; Exact footer' WHERE id='task'")
    refs=plan(store,'task',literals=[{'role':'header','quote':'Exact header'},
                                   {'role':'footer','quote':'Exact footer'}])['literals']
    proposal={'tool':'word.create','path':'report.docx',
              'blocks':[{'kind':'paragraph','text':'Body','style':'BodyStyle'}],
              'header':{'text':'Exact header'},'footer':{'text':'Exact footer'}}
    hint=capture(store,'task',proposal,allow_valid=request.param=='word.finalize_creation')
    action={'tool':request.param,'ref':hint['ref'],
            'styles':[{'id':'BodyStyle','font':'Arial','size_points':11}],
            'bindings':[{'slot':item['role']+'.text','ref':item['ref']} for item in refs]}
    data=package(para('Body'))
    monkeypatch.setattr(WordPlugin,'_execute',lambda *args,**kwargs: ({'candidate_revision':digest(data)},data))

    def stage(call_action=None):
        submitted=copy.deepcopy(action if call_action is None else call_action)
        call=record_call(store,'task',submitted)
        receipt=getattr(WordPlugin(store),submitted['tool'].split('.')[1])(
            **{key:value for key,value in submitted.items() if key!='tool'},
            task_id='task',idempotency_key=operation_key(call))
        def forbidden(*args,**kwargs):
            pytest.fail('Recovery must not resolve, bind, or execute')
        monkeypatch.setattr(WordPlugin,'_execute',forbidden)
        monkeypatch.setattr('localoffice.word.creation_repair.resolve',forbidden)
        monkeypatch.setattr('localoffice.word.content_plan.resolve',forbidden)
        monkeypatch.setattr('localoffice.word.content_plan.bind_creation',forbidden)
        return call,receipt
    yield store,action,stage
    store.close()


def snapshot(store,receipt):
    return dict(store.operation(receipt['receipt_id']))


def rewrite_saved(store,receipt,edit,*,rehash):
    row=snapshot(store,receipt);saved=json.loads(row['args']);edit(saved['repair_request'])
    encoded=canonical(saved)
    with store.transaction():
        store.db.execute('UPDATE operations SET args=?,args_hash=? WHERE id=?',
                         (encoded,digest(encoded.encode()) if rehash else row['args_hash'],row['id']))


def test_typed_recovery_after_restart_uses_ledger_without_current_sources(context):
    store,action,stage=context
    call,receipt=stage();before=snapshot(store,receipt)
    saved=json.loads(before['args'])
    assert saved['repair_request']['bindings']==action['bindings']
    assert saved['repair_request']['source_bindings']=={}
    assert saved['request']['header']['text']=='Exact header'
    assert saved['request']['footer']['text']=='Exact footer'
    original_function=store.db.execute('SELECT function FROM native_calls WHERE call_id=?',(call,)).fetchone()[0]
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective='Sources now changed' WHERE id='task'")
    root=store.root;store.close();reopened=ProjectStore(root)
    try:
        assert reconcile_word_call(reopened,'task')['receipt_id']==receipt['receipt_id']
        assert snapshot(reopened,receipt)==before
        pair=exchanges(reopened,'task')[0]
        assert canonical(pair[0]['tool_calls'][0]['function'])==original_function
        assert json.loads(pair[1]['content'])==receipt
        assert reconcile_word_call(reopened,'task')['state']=='no_pending_call'
    finally:reopened.close()


@pytest.mark.parametrize('form',['absent','empty','legacy'])
def test_historical_empty_typed_field_omission_is_compatible(context,form):
    store,action,stage=context
    bindings=action.pop('bindings')
    if form=='empty':action['bindings']=[]
    if form=='legacy':action['source_bindings']={item['slot']:item['ref'] for item in bindings}
    call,receipt=stage(action)
    rewrite_saved(store,receipt,lambda saved:saved.pop('bindings'),rehash=True)
    before=snapshot(store,receipt)
    assert reconcile_word_call(store,'task')['state']=='reconciled'
    assert snapshot(store,receipt)==before
    assert exchanges(store,'task')[0][0]['tool_calls'][0]['function']['arguments']=={
        key:value for key,value in action.items() if key!='tool'}


@pytest.mark.parametrize('change',['legacy_map','reverse','different_ref','different_slot','empty'])
def test_nonempty_typed_representation_is_not_normalized_away(context,change):
    store,action,stage=context
    call,receipt=stage()
    def edit(saved):
        if change=='legacy_map':
            saved['source_bindings']={item['slot']:item['ref'] for item in saved.pop('bindings')}
        elif change=='reverse':saved['bindings'].reverse()
        elif change=='different_ref':saved['bindings'][0]['ref']='ct_'+'f'*24
        elif change=='different_slot':saved['bindings'][0]['slot']='first_header.text'
        else:saved['bindings']=[]
    rewrite_saved(store,receipt,edit,rehash=True)
    before=snapshot(store,receipt)
    with pytest.raises(HarnessError) as error:reconcile_word_call(store,'task')
    assert error.value.code=='native_history'
    assert snapshot(store,receipt)==before
    assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(call,)).fetchone()[0] is None


def test_hash_checked_before_empty_typed_compatibility(context):
    store,action,stage=context
    action['bindings']=[]
    call,receipt=stage(action)
    rewrite_saved(store,receipt,lambda saved:saved.pop('bindings'),rehash=False)
    before=snapshot(store,receipt)
    with pytest.raises(HarnessError) as error:reconcile_word_call(store,'task')
    assert error.value.code=='state_corrupt'
    assert snapshot(store,receipt)==before
    assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(call,)).fetchone()[0] is None
