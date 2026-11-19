import json
import pytest
from localoffice.storage import ProjectStore,canonical
from localoffice.runtime import TaskRunner
from localoffice.recovery import load,PREFIX
from localoffice.errors import HarnessError
from test_word_recovery_budget import Scripted
from test_word_tables import saved_table
from test_word_text_format import get
from test_word_progress import task


def fail(store,key='task'):
    return store.message('tool',canonical({'code':'stale_object','message':'Rejected edit'}),key,
                         recovery_state={'unresolved':True,'mutation':True,'failures_in_run':1})


def edit(plugin,rev,key='correct',task_id='task'):
    obj=get(plugin,rev)
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'format_text','target':obj['id'],'expected_hash':obj['hash'],'start':0,'end':3,'properties':{'bold':True}}],idempotency_key=key,task_id=task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('correct',[False,True])
async def test_pending_mutation_survives_continue_restart_and_history_pruning(tmp_path,correct):
    store,plugin,original,rev=saved_table(tmp_path);obj=get(plugin,rev)
    bad={'tool':'word.format_text','path':'report.docx','revision':rev,'target':obj['id'],'expected_hash':'0'*64,'start':0,'end':3,'properties':{'bold':True}}
    first=TaskRunner(store,Scripted([bad,{'tool':'ask_user','text':'Review the failed edit.'}]))
    key=await first.start('Bold the first three letters of the introduction',artifact='report.docx',mode='background');await first.job
    chat=first.chat_id
    assert load(store,key,chat)['unresolved']
    snapshot=store.checkpoint(key)
    assert snapshot['recovery'][0]['task_id']==key and snapshot['recovery'][0]['mutation']
    with store.transaction():store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('history_floor:'+chat,str(snapshot['history_cursor'])))
    await first.pause();store.close();store=ProjectStore(tmp_path)
    finish={'tool':'finish','text':'Done'}
    actions=[{'tool':'word.load_tools','operations':['format_text']},{**bad,'expected_hash':obj['hash']},finish] if correct else [finish,finish]
    class Provider(Scripted):
        async def stream(self,messages,**kwargs):
            if self.calls==0:assert '"pending_recovery"' in messages[0]['content']
            async for event in super().stream(messages,**kwargs):yield event
    second=TaskRunner(store,Provider(actions))
    try:
        await second.start('Continue',task_id=key,artifact='report.docx',mode='background');await second.job
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]
        assert state==('completed' if correct else 'failed')
        assert load(store,key,chat)['unresolved']==(not correct)
        assert bool(store.candidate('report.docx'))==correct
        assert (tmp_path/'report.docx').read_bytes()==original
        assert store.db.execute("SELECT count(*) FROM operations WHERE task_id=? AND status='validated'",(key,)).fetchone()[0]==int(correct)
    finally:await second.pause();store.close()


def test_crash_after_validation_reconciles_without_duplicate_edit(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store);fail(store)
    result=edit(plugin,rev)
    assert load(store,'task',chat,reconcile=False)['unresolved']
    count=store.db.execute('SELECT count(*) FROM operations').fetchone()[0]
    store.close();store=ProjectStore(tmp_path)
    try:
        state=load(store,'task',chat)
        assert not state['unresolved'] and state['resolved_by']==result['receipt_id']
        assert load(store,'task',chat)==state
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==count
        assert (tmp_path/'report.docx').read_bytes()==original
        assert store.candidate('report.docx')['candidate_hash']==result['candidate_revision']
    finally:store.close()


def test_old_receipt_revalidation_and_other_task_do_not_resolve_failure(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store)
    prior=edit(plugin,rev);fail(store)
    try:
        with store.transaction():store.event(prior['receipt_id'],'validated',{'recovered':True})
        assert load(store,'task',chat)['unresolved']
        task(store,'other')
        # Even a later matching receipt from another task has no recovery authority.
        obj=get(plugin,prior['candidate_revision'])
        plugin.stage_batch(path='report.docx',revision=prior['candidate_revision'],operations=[{'op':'format_text','target':obj['id'],'expected_hash':obj['hash'],'start':4,'end':7,'properties':{'italic':True}}],idempotency_key='other-edit',task_id='other')
        assert load(store,'task',chat)['unresolved']
        with pytest.raises(HarnessError,match='another chat'):load(store,'task','wrong-chat')
    finally:store.close()


def test_error_message_and_recovery_record_are_one_transaction(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    try:
        before=store.db.execute('SELECT count(*) FROM messages').fetchone()[0]
        with store.transaction():store.db.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON meta WHEN NEW.key LIKE 'task_recovery:%' BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        with pytest.raises(Exception,match='simulated failure'):fail(store)
        assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0]==before
        assert store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+'task',)).fetchone() is None
    finally:store.close()


def test_mismatched_or_corrupt_current_candidate_cannot_resolve(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store);fail(store)
    result=edit(plugin,rev)
    try:
        with store.transaction():store.db.execute("UPDATE candidates SET candidate_hash=? WHERE path='report.docx'",(rev,))
        assert load(store,'task',chat)['unresolved']
        with store.transaction():store.db.execute("UPDATE candidates SET candidate_hash=? WHERE path='report.docx'",(result['candidate_revision'],))
        (store.blobs/result['candidate_revision']).write_bytes(b'corrupted')
        with pytest.raises(HarnessError,match='hash check'):load(store,'task',chat)
        assert load(store,'task',chat,reconcile=False)['unresolved']
    finally:store.close()


def test_corrupt_recovery_record_is_not_silently_ignored(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store)
    try:
        with store.transaction():store.db.execute('INSERT INTO meta VALUES(?,?)',(PREFIX+'task','{}'))
        with pytest.raises(HarnessError,match='recovery state is invalid'):load(store,'task',chat)
    finally:store.close()


def test_explicit_memory_forget_removes_recovery_facts(tmp_path):
    from localoffice.memory import forget_project_memory
    store,plugin,original,rev=saved_table(tmp_path);task(store);fail(store)
    try:
        assert store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+'task',)).fetchone()
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        assert store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'task_recovery:%'").fetchone() is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('external_change',[False,True])
def test_committed_correction_requires_matching_current_source(tmp_path,external_change):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store);fail(store)
    result=edit(plugin,rev)
    store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
    assert store.candidate('report.docx') is None
    if external_change:(tmp_path/'report.docx').write_bytes(original)
    store.close();store=ProjectStore(tmp_path)
    try:
        state=load(store,'task',chat)
        assert state['unresolved']==external_change
        if not external_change:assert state['resolved_by']==result['receipt_id']
        assert store.candidate('report.docx') is None
        assert store.db.execute("SELECT count(*) FROM operations WHERE task_id='task'").fetchone()[0]==1
    finally:store.close()
