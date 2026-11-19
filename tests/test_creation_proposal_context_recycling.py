"""Fresh proposal recycling releases only reloadable tools, never proposal evidence."""
import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical,digest
from localoffice.errors import HarnessError
from localoffice.native_history import record_call,record_result
from localoffice.word.creation_repair import capture,pending,PREFIX
from localoffice.word.requirements import plan
from localoffice.word.guidance import build_state,load,loaded_receipt,selected
from test_word_content_plan import fixture

CHOICES=['edit_creation_blocks','revise_creation','finalize_creation']
OBJECTIVE='Keep exact Δ. Use one-inch margins.'


class Pressure:
    native_tool_protocol=True
    context_limit=4096
    def __init__(self,always=False):self.always=always;self.counts=[]
    def status(self):return {'loaded':True,'constrained_decoding':True}
    async def count_tokens(self,messages,*,tools):
        names={t['function']['name'] for t in tools}
        self.counts.append(names)
        active=json.loads(messages[0]['content'].split('Active Word operations: ',1)[1].split('\n',1)[0])
        return 3500 if self.always or active.get('operations')==CHOICES else 1000


def setup(store,status='prepared'):
    blocks=[{'kind':'paragraph','text':'Keep exact Δ.'}]
    if status=='invalid_design':blocks.append({'kind':'page_break'})
    if status=='missing_styles':blocks[0]['style']='Missing'
    proposal=capture(store,'task',{'path':'report.docx','blocks':blocks},allow_valid=status!='missing_styles')
    plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=[{
        'kind':'page_geometry','source_quote':'one-inch margins','field':'margin','equals':1.0,'unit':'in'}])
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(CHOICES))))
    identity=record_call(store,'task',{'tool':'word.finalize_creation','ref':proposal['ref'],'styles':[]})
    record_result(store,'task',identity,{'code':'requirements_mismatch','message':'Keep this exact latest failure.',
        'recovery':{'unabridged_evidence':'Selected constraint not met.'}})
    return proposal


def exact(messages):
    return json.loads(messages[0]['content'].split('Current authoritative state: ',1)[1].split('\nActive Word operations:',1)[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['prepared','invalid_design'])
async def test_recycle_preserves_proposal_requirements_objective_error_and_single_reload(tmp_path,status):
    store=fixture(tmp_path,OBJECTIVE);setup(store,status)
    runner=TaskRunner(store,Pressure(),source_planning=True,creation_review=True)
    try:
        prior=pending(store,'task')
        immutable=[tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key NOT LIKE 'word_tools:%' ORDER BY key")]
        messages=await runner.assemble('task');state=exact(messages)
        assert any(m['role']=='user' and m['content']==OBJECTIVE for m in messages)
        assert state['creation_repair']==prior
        assert state['creation_repair']['selected_requirements']['status']==('unverified' if status=='invalid_design' else 'mismatch')
        assert state['target']=={'path':'report.docx','exists_on_disk':False,'expected_revision':None}
        assert state['edit_scope'] is None
        assert 'Keep this exact latest failure.' in messages[-1]['content']
        assert 'Selected constraint not met.' in messages[-1]['content']
        assert selected(store,'task')['operations']==['revise_creation']
        assert runner.context_info['recycle_reason']=='fresh_retained_proposal'
        assert runner.context_info['evicted_word_operations']==['edit_creation_blocks','finalize_creation']
        assert runner.context_info['retained_word_operations']==['revise_creation']
        assert immutable==[tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key NOT LIKE 'word_tools:%' ORDER BY key")]
        loader=next(t['function'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
        assert set(CHOICES+['prepare_creation','plan_requirements','inspect'])<=set(loader['parameters']['properties']['operations']['items']['enum'])
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        row=store.db.execute("SELECT body FROM task_measurements WHERE kind='context_recycle'").fetchone()
        assert json.loads(row[0])['reason']=='fresh_retained_proposal'
        # Normal preflight plus persistence of an explicit single-tool reload.
        single=build_state(['revise_creation'])
        await runner.assemble('task',word_tool_override=single,pending_tool_result=loaded_receipt(single))
        store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
        load(store,'task',['revise_creation'])
        await runner.assemble('task')
        assert runner.word_tool_state['operations']==['revise_creation']
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['missing','stale','corrupt','missing_styles','reduced_overflow','override','pending_result'])
async def test_unqualified_or_failed_reduction_does_not_persist_tool_eviction(tmp_path,case):
    store=fixture(tmp_path,OBJECTIVE)
    if case!='missing':setup(store,'missing_styles' if case=='missing_styles' else 'prepared')
    else:store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(CHOICES))))
    if case=='stale':store.db.execute("UPDATE tasks SET objective='New objective' WHERE id='task'")
    if case=='corrupt':
        row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+'task',)).fetchone()
        state=json.loads(row[0]);state['hash']='0'*64
        store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(state),PREFIX+'task'))
    prior=store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0]
    provider=Pressure(always=case in ('stale','reduced_overflow'))
    runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        kwargs={'word_tool_override':build_state(CHOICES)} if case=='override' else {'pending_tool_result':{'evidence':'pending'}} if case=='pending_result' else {}
        with pytest.raises(HarnessError) as error:await runner.assemble('task',**kwargs)
        assert error.value.code==('state_corrupt' if case=='corrupt' else 'context_capacity')
        assert store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0]==prior
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        if case=='reduced_overflow':
            assert runner.word_tool_state['operations']==CHOICES
            assert {'word.'+n for n in CHOICES}<={t['function']['name'] for t in runner.native_tools}
            assert runner.context_info['input_tokens']==3500
            assert len(provider.counts)<10  # One reduced attempt; no recursion loop.
    finally:await runner.pause();store.close()
