import pytest
from localoffice.storage import ProjectStore,digest
from localoffice.errors import HarnessError
from localoffice.native_history import record_call
from localoffice.word import requirements as req
from localoffice.word.plugin import WordPlugin
from test_word_progress import task

RAW={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}]}

@pytest.fixture(params=sorted(req.PLANNING_MODES))
def ctx(tmp_path,request):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute('INSERT INTO meta VALUES(?,?)',('task_content_mode:task',request.param))
    yield store,WordPlugin(store)
    store.close()

def clear_plan(store):
    text=store.db.execute("SELECT objective FROM tasks WHERE id='task'").fetchone()[0]
    return req.plan(store,'task',source_hash=digest(text.encode()),requirements=[])

@pytest.mark.parametrize('mode',[None,'direct','source_planning','reviewed_source_planning'])
def test_other_modes_unchanged(ctx,mode):
    store,plugin=ctx
    with store.transaction():
        store.db.execute('DELETE FROM meta WHERE key=?',('task_content_mode:task',))
        if mode:store.db.execute('INSERT INTO meta VALUES(?,?)',('task_content_mode:task',mode))
    assert req.require_planned(store,'task') is None
    assert plugin.prepare_creation(task_id='task',**RAW)['status']=='prepared'

@pytest.mark.parametrize('kind',['prepare','create','finalize'])
@pytest.mark.parametrize('reason',['missing','stale'])
def test_skip_blocked_before_worker(ctx,monkeypatch,kind,reason):
    store,plugin=ctx
    if kind=='finalize':
        clear_plan(store);ref=plugin.prepare_creation(task_id='task',**RAW)['ref']
    if reason=='stale':
        clear_plan(store)
        with store.transaction():store.db.execute("UPDATE tasks SET objective='New objective' WHERE id='task'")
    else:
        with store.transaction():store.db.execute('DELETE FROM meta WHERE key=?',(req.PREFIX+'task',))
    monkeypatch.setattr(plugin,'_execute',lambda *a,**k:pytest.fail('Worker must not run'))
    with pytest.raises(HarnessError) as exc:
        if kind=='prepare':plugin.prepare_creation(task_id='task',**RAW)
        elif kind=='create':plugin.create(task_id='task',idempotency_key='new',**RAW)
        else:plugin.finalize_creation(task_id='task',idempotency_key='new',ref=ref,styles=[])
    # Finalize already validates the retained proposal source before _create.
    if kind=='finalize' and reason=='stale':assert exc.value.code=='creation_repair_stale'
    else:
        assert exc.value.code=='requirements_planning_required'
        assert exc.value.recovery['reason']==reason
        assert exc.value.recovery['operations']==['plan_requirements']
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    assert store.candidate('report.docx') is None


def test_empty_plan_and_native_prepare_historical_replay(ctx,monkeypatch):
    store,plugin=ctx
    assert clear_plan(store)['status']=='unassessed'
    assert req.require_planned(store,'task')['request']['requirements']==[]
    call=record_call(store,'task',{'tool':'word.prepare_creation',**RAW})
    receipt=plugin.prepare_creation(task_id='task',native_call_id=call,**RAW)
    assert 'selected_requirements' not in receipt
    with store.transaction():store.db.execute('DELETE FROM meta WHERE key=?',(req.PREFIX+'task',))
    monkeypatch.setattr(req,'require_planned',lambda *a,**k:pytest.fail('Historical replay must bypass new gate'))
    assert plugin.prepare_creation(task_id='task',native_call_id=call,**RAW)==receipt


def test_operation_replay_outranks_new_requirement(ctx,monkeypatch):
    store,plugin=ctx;clear_plan(store)
    receipt=plugin.create(task_id='task',idempotency_key='first',**RAW)
    with store.transaction():store.db.execute('DELETE FROM meta WHERE key=?',(req.PREFIX+'task',))
    monkeypatch.setattr(req,'require_planned',lambda *a,**k:pytest.fail('Historical replay must bypass new gate'))
    monkeypatch.setattr(plugin,'_execute',lambda *a,**k:pytest.fail('Replay must not execute'))
    assert plugin.create(task_id='task',idempotency_key='first',**RAW)==receipt
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
