"""Prefer the generic proposal revision tool on real counted context admission."""
import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.errors import HarnessError
from localoffice.storage import canonical
from localoffice.native_tools import validate_history
from localoffice.word.creation_repair import pending
from localoffice.word.guidance import build_state,selected
from test_word_content_plan import fixture
from test_creation_proposal_context_recycling import setup,exact,OBJECTIVE,CHOICES


class Pressure:
    native_tool_protocol=True
    context_limit=4096
    def __init__(self,mode='retain'):self.mode=mode;self.counts=[]
    def status(self):return {'loaded':True,'constrained_decoding':True}
    async def count_tokens(self,messages,*,tools):
        validate_history(messages)
        names=json.loads(messages[0]['content'].split('Active Word operations: ',1)[1].split('\n',1)[0])['operations']
        self.counts.append(names)
        if names==['revise_creation'] and self.mode in ('corrupt','unexpected'):
            if self.mode=='corrupt':raise HarnessError('state_corrupt','Injected candidate integrity failure')
            raise RuntimeError('Injected unexpected failure')
        if self.mode=='always' or names==CHOICES:return 3500
        if names==['revise_creation'] and self.mode in ('empty','single'):return 3500
        return 3100 if names else 1000


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['prepared','invalid_design'])
async def test_retained_revision_preserves_full_evidence_and_other_lazy_tools(tmp_path,status):
    store=fixture(tmp_path,OBJECTIVE);setup(store,status)
    provider=Pressure();runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        proposal=pending(store,'task')
        original=[tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key NOT LIKE 'word_tools:%' ORDER BY key")]
        messages=await runner.assemble('task');state=exact(messages)
        assert selected(store,'task')['operations']==['revise_creation']
        assert runner.context_info['input_tokens']==3100
        assert runner.context_info['retained_word_operations']==['revise_creation']
        assert runner.context_info['evicted_word_operations']==['edit_creation_blocks','finalize_creation']
        assert state['creation_repair']==proposal
        assert state['selected_requirement_plan']['requirements']
        assert state['target']['expected_revision'] is None and state['edit_scope'] is None
        assert any(m['role']=='user' and m['content']==OBJECTIVE for m in messages)
        assert 'Keep this exact latest failure.' in messages[-1]['content']
        assert 'Selected constraint not met.' in messages[-1]['content']
        names={t['function']['name'] for t in runner.native_tools}
        assert 'word.revise_creation' in names and 'word.edit_creation_blocks' not in names
        loader=next(t['function'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
        assert set(CHOICES+['prepare_creation','inspect','plan_requirements'])<=set(loader['parameters']['properties']['operations']['items']['enum'])
        assert original==[tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key NOT LIKE 'word_tools:%' ORDER BY key")]
        metric=json.loads(store.db.execute("SELECT body FROM task_measurements WHERE kind='context_recycle'").fetchone()[0])
        assert metric['retained_operations']==['revise_creation'] and metric['reason']=='fresh_retained_proposal'
        assert [] not in provider.counts
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['empty','single'])
async def test_context_capacity_falls_back_empty_and_does_not_retry_same_single_tool(tmp_path,mode):
    store=fixture(tmp_path,OBJECTIVE);setup(store)
    if mode=='single':store.db.execute("UPDATE meta SET value=? WHERE key='word_tools:task'",(canonical(build_state(['revise_creation'])),))
    provider=Pressure(mode);runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    class Recorder:
        attempts=[]
    original=runner.assemble
    async def recording(key,**kwargs):
        if kwargs.get('word_tool_override') is not None:Recorder.attempts.append(kwargs['word_tool_override']['operations'])
        return await original(key,**kwargs)
    runner.assemble=recording
    try:
        await runner.assemble('task')
        assert Recorder.attempts==([['revise_creation'],[]] if mode=='empty' else [[]])
        assert selected(store,'task')['operations']==[]
        assert runner.context_info['retained_word_operations']==[]
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['always','corrupt','unexpected'])
async def test_failed_reduction_restores_context_tools_and_never_persists_selection(tmp_path,mode):
    store=fixture(tmp_path,OBJECTIVE);setup(store)
    provider=Pressure(mode);runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    before=store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0]
    try:
        with pytest.raises(RuntimeError if mode=='unexpected' else HarnessError) as exc:
            await runner.assemble('task')
        if mode!='unexpected':assert exc.value.code==('context_capacity' if mode=='always' else 'state_corrupt')
        assert store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0]==before
        assert runner.word_tool_state['operations']==CHOICES
        assert runner.context_info['input_tokens']==3500
        assert 'word.edit_creation_blocks' in {t['function']['name'] for t in runner.native_tools}
        if mode!='always':assert [] not in provider.counts
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('guard',['override','pending_result'])
async def test_explicit_admission_does_not_recycle(tmp_path,guard):
    store=fixture(tmp_path,OBJECTIVE);setup(store)
    runner=TaskRunner(store,Pressure(),source_planning=True,creation_review=True)
    try:
        kwargs={'word_tool_override':build_state(CHOICES)} if guard=='override' else {'pending_tool_result':{'status':'pending'}}
        with pytest.raises(HarnessError,match='cannot fit'):await runner.assemble('task',**kwargs)
        assert selected(store,'task')['operations']==CHOICES
    finally:await runner.pause();store.close()
