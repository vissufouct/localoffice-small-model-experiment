"""Absent/null restart keeps historical list calls byte-shape compatible."""
import json
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import canonical
from localoffice.word.contracts import StageBatch
from localoffice.native_history import record_call, operation_key, reconcile_word_call, exchanges
from localoffice.word.plugin import WordPlugin
from test_word_lists import get, restart_list
from test_word_tables import saved_table
from test_word_progress import task


@pytest.mark.parametrize('explicit_null',[False,True])
def test_historical_list_replay_and_native_recovery_missing_null(tmp_path,monkeypatch,explicit_null):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        task(store);target=get(plugin,revision,'paragraph:2')
        op={'id':'list','op':'set_list','target':target['id'],'expected_hash':target['hash'],'number_id':2,'level':0}
        action={'tool':'word.stage_batch','path':'report.docx','revision':revision,'operations':[op]}
        if explicit_null:op['restart_at']=None
        normalized=StageBatch(**action).model_dump()
        assert 'restart_at' not in normalized['operations'][0]
        assert 'restart_at' not in json.loads(StageBatch(**action).model_dump_json())['operations'][0]
        call=record_call(store,'task',action)
        result=plugin.stage_batch(**{k:v for k,v in action.items() if k!='tool'},task_id='task',idempotency_key=operation_key(call))
        before=dict(store.operation(result['receipt_id']))
        assert 'restart_at' not in json.loads(before['args'])['request']['operations'][0]
        def forbidden(*a,**kw):pytest.fail('Replay must not execute')
        monkeypatch.setattr(WordPlugin,'_execute',forbidden)
        op['restart_at']=None
        assert plugin.stage_batch(**{k:v for k,v in action.items() if k!='tool'},task_id='task',idempotency_key=operation_key(call))==result
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        assert dict(store.operation(result['receipt_id']))==before
        op['restart_at']=1
        with pytest.raises(HarnessError) as exc:plugin.stage_batch(**{k:v for k,v in action.items() if k!='tool'},task_id='task',idempotency_key=operation_key(call))
        assert exc.value.code=='idempotency_conflict'
    finally:store.close()


def test_restart_native_recovery_allocates_once_and_rejects_tampered_hash(tmp_path,monkeypatch):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        task(store);target=get(plugin,revision,'paragraph:2')
        action={'tool':'word.stage_batch','path':'report.docx','revision':revision,'operations':[{'id':'restart','op':'set_list','target':target['id'],'expected_hash':target['hash'],'number_id':2,'level':0,'restart_at':5}]}
        call=record_call(store,'task',action)
        result=restart_list(plugin,revision,target,5,key=operation_key(call),task_id='task')
        before=dict(store.operation(result['receipt_id']))
        monkeypatch.setattr(WordPlugin,'_execute',lambda *a,**k:pytest.fail('No allocation on replay'))
        assert restart_list(plugin,revision,target,5,key=operation_key(call),task_id='task')==result
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        projected=json.loads(exchanges(store,'task')[-1][1]['content'])
        assert projected['results'][0]['number_id']==3
        assert dict(store.operation(result['receipt_id']))==before
        saved=json.loads(before['args']);saved['request']['operations'][0]['restart_at']=6
        with store.transaction():store.db.execute('UPDATE operations SET args=? WHERE id=?',(canonical(saved),result['receipt_id']))
        with pytest.raises(HarnessError) as exc:restart_list(plugin,revision,target,5,key=operation_key(call),task_id='task')
        assert exc.value.code=='state_corrupt'
    finally:store.close()
