import pytest
from localoffice.native_history import record_call,operation_key,reconcile_word_call,exchanges
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.errors import HarnessError
from test_word_tables import saved_table
from test_word_progress import task


def prepare(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path);task(store)
    obj=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:2'])['objects'][0]
    action={'tool':'word.stage_batch','path':'report.docx','revision':revision,'operations':[
        {'op':'format_text','target':obj['id'],'expected_hash':obj['hash'],'start':0,'end':3,'properties':{'bold':True}}]}
    call=record_call(store,'task',action)
    return store,plugin,original,action,call


def execute(plugin,action,call):
    return plugin.stage_batch(**{k:v for k,v in action.items() if k!='tool'},task_id='task',idempotency_key=operation_key(call))


def test_missing_receipt_recovers_after_restart_without_worker_replay(tmp_path,monkeypatch):
    store,plugin,original,action,call=prepare(tmp_path)
    receipt=execute(plugin,action,call)
    store.close();store=ProjectStore(tmp_path)
    try:
        def forbidden(*args,**kwargs):raise AssertionError('Recovery must not invoke a worker')
        monkeypatch.setattr(WordPlugin,'_execute',forbidden)
        result=reconcile_word_call(store,'task')
        assert result['state']=='reconciled' and result['receipt_id']==receipt['receipt_id']
        assert exchanges(store,'task')[0][1]['tool_call_id']==call
        assert reconcile_word_call(store,'task')['state']=='no_pending_call'
        assert store.db.execute("SELECT count(*) FROM operations WHERE task_id='task'").fetchone()[0]==1
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_missing_ledger_does_not_infer_execution(tmp_path):
    store,plugin,original,action,call=prepare(tmp_path)
    try:
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        assert store.candidate('report.docx') is None
        assert store.db.execute('SELECT result_message FROM native_calls').fetchone()[0] is None
    finally:store.close()


def test_different_arguments_with_same_key_cannot_close_call(tmp_path):
    store,plugin,original,action,call=prepare(tmp_path)
    action['operations'][0]['properties']={'italic':True}
    execute(plugin,action,call)
    try:
        with pytest.raises(HarnessError,match='different recorded arguments'):reconcile_word_call(store,'task')
        assert store.db.execute('SELECT result_message FROM native_calls').fetchone()[0] is None
    finally:store.close()


def test_applied_then_external_change_remains_unresolved(tmp_path):
    store,plugin,original,action,call=prepare(tmp_path)
    result=execute(plugin,action,call)
    store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
    (tmp_path/'report.docx').write_bytes(original)
    try:assert reconcile_word_call(store,'task')['state']=='unresolved'
    finally:store.close()


@pytest.mark.parametrize('applied',[False,True])
def test_compact_evidence_call_recovers_before_or_after_apply(tmp_path,applied):
    store,plugin,original,revision=saved_table(tmp_path);task(store)
    obj=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:2'],task_id='task')['objects'][0]
    action={'tool':'word.format_text','path':'report.docx','revision':revision,'target':obj['id'],
            'expected_hash':obj['evidence_ref'],'start':0,'end':3,'properties':{'bold':True}}
    call=record_call(store,'task',action)
    from localoffice.word.contracts import FormatTextCall
    batch=FormatTextCall(**action).as_batch().model_dump()
    receipt=execute(plugin,batch,call)
    if applied:store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply')
    try:
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        assert len(exchanges(store,'task'))==1
    finally:store.close()


def test_corrupt_candidate_cannot_supply_recovered_receipt(tmp_path):
    store,plugin,original,action,call=prepare(tmp_path)
    receipt=execute(plugin,action,call)
    (store.blobs/receipt['candidate_revision']).write_bytes(b'corrupt')
    try:
        with pytest.raises(HarnessError):reconcile_word_call(store,'task')
        assert store.db.execute('SELECT result_message FROM native_calls').fetchone()[0] is None
    finally:store.close()
