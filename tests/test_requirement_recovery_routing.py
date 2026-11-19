"""Recover exact requirement receipts without overriding current user tool choices."""
import json
import pytest
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.native_history import record_call,reconcile_word_call
from localoffice.word import requirements as req
from localoffice.word.guidance import build_state,load
from test_word_progress import task

@pytest.fixture
def ctx(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective='Use one inch margins.' WHERE id='task'")
        store.db.execute('INSERT INTO meta VALUES(?,?)',('task_content_mode:task','requirements_reviewed_source_planning'))
        store.db.execute('INSERT INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(['plan_requirements']))))
    yield store
    store.close()

def execute(store,empty=False):
    args={'source_hash':digest(b'Use one inch margins.'),'requirements':[] if empty else [
        {'kind':'page_geometry','source_quote':'one inch','field':'margin','equals':1,'unit':'in'}]}
    call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
    receipt=req.plan(store,'task',**args,native_call_id=call)
    return call,receipt,args

def selected(store):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',('word_tools:task',)).fetchone()
    return row[0] if row else None

def assert_receipt(store,call,receipt):
    row=store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(call,)).fetchone()
    assert json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(row[0],)).fetchone()[0])==receipt
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0

@pytest.mark.parametrize('empty',[False,True])
def test_restart_restores_fresh_current_generation(ctx,empty):
    call,receipt,args=execute(ctx,empty);root=ctx.root
    plan_before=req.get(ctx,'task');ctx.close()
    store=ProjectStore(root)
    try:
        result=reconcile_word_call(store,'task')
        assert result['routing_restored'] is True
        assert json.loads(selected(store))['operations']==['prepare_creation']
        assert req.get(store,'task')==plan_before
        assert_receipt(store,call,receipt)
    finally:store.close()

@pytest.mark.parametrize('mode',['direct','source_planning','reviewed_source_planning',None])
def test_manual_default_workflows_preserve_selection(ctx,mode):
    call,receipt,args=execute(ctx)
    with ctx.transaction():
        ctx.db.execute('DELETE FROM meta WHERE key=?',('task_content_mode:task',))
        if mode:ctx.db.execute('INSERT INTO meta VALUES(?,?)',('task_content_mode:task',mode))
    before=selected(ctx)
    assert reconcile_word_call(ctx,'task')['routing_restored'] is False
    assert selected(ctx)==before
    assert_receipt(ctx,call,receipt)

@pytest.mark.parametrize('reason',['stale','superseded','disk','candidate'])
def test_historical_receipt_never_reactivates_old_plan(ctx,reason):
    call,receipt,args=execute(ctx)
    if reason=='stale':
        with ctx.transaction():ctx.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    elif reason=='superseded':req.plan(ctx,'task',source_hash=args['source_hash'],requirements=[])
    elif reason=='disk':(ctx.root/'report.docx').write_bytes(b'Existing target')
    else:
        # A pending working copy blocks creation even while the disk target is absent.
        with ctx.transaction():ctx.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',('report.docx','op',None,'a'*64))
    before=selected(ctx)
    assert reconcile_word_call(ctx,'task')['routing_restored'] is False
    assert selected(ctx)==before
    assert_receipt(ctx,call,receipt)

@pytest.mark.parametrize('operations',[['inspect'],['replace_text'],[],['plan_requirements','inspect']])
def test_preserves_newer_or_mixed_selection(ctx,operations):
    call,receipt,args=execute(ctx)
    with ctx.transaction():ctx.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(build_state(operations)) if operations else canonical({'operations':[],'guidance':{},'source_hash':'recycled'}),'word_tools:task'))
    before=selected(ctx)
    assert reconcile_word_call(ctx,'task')['routing_restored'] is False
    assert selected(ctx)==before
    assert_receipt(ctx,call,receipt)

def test_newer_explicit_same_selection_receipt_prevents_override(ctx):
    call,receipt,args=execute(ctx)
    loaded=load(ctx,'task',['plan_requirements'])
    with ctx.transaction():ctx.message('tool',canonical(loaded),'task')
    before=selected(ctx)
    assert reconcile_word_call(ctx,'task')['routing_restored'] is False
    assert selected(ctx)==before
    assert_receipt(ctx,call,receipt)

def test_unexecuted_call_remains_unresolved(ctx):
    args={'source_hash':digest(b'Use one inch margins.'),'requirements':[]}
    record_call(ctx,'task',{'tool':'word.plan_requirements',**args})
    before=selected(ctx)
    assert reconcile_word_call(ctx,'task')['state']=='unresolved'
    assert selected(ctx)==before
