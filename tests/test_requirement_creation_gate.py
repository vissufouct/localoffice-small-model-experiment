"""Selected requirements guard new execution, never rewrite intent or past receipts."""
import json
import pytest
from localoffice.storage import ProjectStore,digest
from localoffice.errors import HarnessError
from localoffice.word.plugin import WordPlugin
from localoffice.word.requirements import plan
from test_word_progress import task

OBJECTIVE='Use 0.75-inch margins. Draft 5–9 words. Use 14-point type.'


@pytest.fixture
def context(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(OBJECTIVE,'task'))
    plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=[
        {'kind':'page_geometry','source_quote':'0.75-inch margins','field':'margin','equals':0.75,'unit':'in'},
        {'kind':'paragraph_words','source_quote':'5–9 words','paragraph_index':0,'min':5,'max':9}])
    yield store,WordPlugin(store)
    store.close()


@pytest.mark.parametrize('mode',['create','finalize_creation'])
def test_failed_selected_constraints_stop_before_worker(context,monkeypatch,mode):
    store,plugin=context
    raw={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Short.'}]}
    if mode=='finalize_creation':
        proposal=plugin.prepare_creation(task_id='task',**raw)
        assert proposal['selected_requirements']['status']=='mismatch'
        assert proposal['selected_requirements']['failed']==2
        ref=proposal['ref']
    def forbidden(*args,**kwargs):raise AssertionError('A failed constraint must not reach the worker')
    monkeypatch.setattr(plugin,'_execute',forbidden)
    with pytest.raises(HarnessError) as error:
        if mode=='create':plugin.create(task_id='task',idempotency_key='bad',**raw)
        else:plugin.finalize_creation(task_id='task',idempotency_key='bad',ref=ref,styles=[])
    assert error.value.code=='requirements_mismatch'
    assert error.value.recovery['failed']==2
    assert error.value.recovery['coverage']=='unassessed'
    assert store.candidate('report.docx') is None
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_corrected_request_records_proof_and_replay_ignores_later_plan(context,monkeypatch):
    store,plugin=context
    raw={'path':'report.docx','page':{'margin_twips':1080},
         'blocks':[{'kind':'paragraph','text':'Please review this short draft carefully before sharing.'}]}
    first=plugin.create(task_id='task',idempotency_key='good',**raw)
    row=store.db.execute("SELECT * FROM operations WHERE idempotency_key='good'").fetchone()
    saved=json.loads(row['args'])['requirement_verification']
    assert saved['checks']['status']=='all_selected_pass'
    assert saved['checks']['coverage']=='unassessed'
    assert saved['generation']==1
    assert len(saved['plan_hash'])==len(saved['resolved_proposal_hash'])==64
    plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=[
        {'kind':'paragraph_font_size','source_quote':'14-point type','paragraph_index':0,'equals':14}])
    def forbidden(*args,**kwargs):raise AssertionError('Exact operation replay must not execute again')
    monkeypatch.setattr(plugin,'_execute',forbidden)
    assert plugin.create(task_id='task',idempotency_key='good',**raw)==first
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1


def test_stale_selected_plan_cannot_be_treated_as_absent(context,monkeypatch):
    store,plugin=context
    with store.transaction():store.db.execute("UPDATE tasks SET objective='New requirements' WHERE id='task'")
    def forbidden(*args,**kwargs):raise AssertionError('Stale evidence must not execute')
    monkeypatch.setattr(plugin,'_execute',forbidden)
    with pytest.raises(HarnessError) as error:
        plugin.create(task_id='task',idempotency_key='stale',path='report.docx',
            blocks=[{'kind':'paragraph','text':'Text'}])
    assert error.value.code=='requirements_stale'
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
