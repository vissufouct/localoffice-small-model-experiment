"""Scripted main-loop integration, not real-model accuracy evidence."""
import json
import pytest
from localoffice import draft_budget
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical, digest
from localoffice.word import draft_fragments, requirements
from localoffice.word.guidance import build_state
from localoffice.native_tools import validate_history
from test_word_content_plan import fixture
from test_native_runner import NativeProvider

OBJECTIVE='Keep the literal. Draft an invitation to review. Keep the ending.'
DRAFT='Please review the document for accuracy and clarity.'

class DraftProvider(NativeProvider):
    def __init__(self):
        super().__init__(None)
        self.seen=[]
    async def stream(self,messages,*,tools,**kwargs):
        validate_history(messages)
        self.seen.append((messages,tools))
        if self.calls==0:
            assert 'word.plan_draft' in {t['function']['name'] for t in tools}
            action={'tool':'word.plan_draft','source_hash':digest(OBJECTIVE.encode()),
                    'source_range':{'start':'s02','end':'s02'}}
        elif self.calls==1:
            assert {t['function']['name'] for t in tools}=={'word.submit_draft','ask_user'}
            assert messages[1]['content']=='Draft an invitation to review. '
            assert OBJECTIVE not in json.dumps(messages)
            ref=tools[0]['function']['parameters']['properties']['ref']['const']
            action={'tool':'word.submit_draft','ref':ref,'text':DRAFT}
        else:
            assert OBJECTIVE in [m['content'] for m in messages if m['role']=='user']
            assert DRAFT in messages[0]['content']
            assert canonical(messages).count(DRAFT)==1
            assert 'word.prepare_creation' in {t['function']['name'] for t in tools}
            assert 'word.plan_draft' not in {t['function']['name'] for t in tools}
            action={'tool':'ask_user','text':'Ready for the next scripted step?'}
        self.calls+=1
        yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action.pop('tool'),'arguments':action})+'</tool_call>'}
        yield {'type':'finish','reason':'stop'}


def setup_runner(tmp_path,provider=None,*,isolated=False):
    store=fixture(tmp_path,OBJECTIVE)
    requirements.plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=[])
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(['prepare_creation']))))
    runner=TaskRunner(store,provider or DraftProvider(),source_planning=True,creation_review=True,
                      requirements_planning=True,focused_drafting=True,isolated_draft_planning=isolated)
    with store.transaction():store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
    return store,runner


@pytest.mark.asyncio
async def test_plan_submit_parent_handoff_same_loop_and_budget(tmp_path):
    store,runner=setup_runner(tmp_path)
    try:
        await runner.run('task','background')
        assert not [e for e in runner.events if e['type'] in ('error','tool_error')],runner.events
        assert runner.provider.calls==3
        assert draft_budget.get(store,'task')==3
        assert draft_fragments.get(store,'task')['text']==DRAFT
        stored=[json.loads(row[0]) for row in store.db.execute('SELECT function FROM native_calls')]
        assert next(c for c in stored if c['name']=='word.submit_draft')['arguments']['text']==DRAFT
        assert store.db.execute("SELECT objective FROM tasks WHERE id='task'").fetchone()[0]==OBJECTIVE
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert not (store.root/'report.docx').exists()
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE task_id='task' AND kind='inference'").fetchone()[0]==3
    finally:store.close()


@pytest.mark.asyncio
async def test_shared_limit_survives_resume_and_draft_handoff(tmp_path):
    store,runner=setup_runner(tmp_path)
    runner.max_steps=2
    try:
        await runner.run('task','background')
        assert runner.provider.calls==2 and draft_budget.get(store,'task')==2
        assert draft_fragments.get(store,'task')['status']=='drafted'
        await runner.run('task','background')
        assert runner.provider.calls==2 and draft_budget.get(store,'task')==2
        assert any(e.get('code')=='step_limit' for e in runner.events)
    finally:store.close()


@pytest.mark.asyncio
async def test_opt_in_mode_cannot_silently_change_on_resume(tmp_path):
    store,runner=setup_runner(tmp_path)
    try:
        await runner.run('task','background')
        provider=DraftProvider()
        changed=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=True)
        await changed.run('task','background')
        assert provider.calls==0
        assert any(e.get('code')=='content_mode_change' for e in changed.events)
    finally:store.close()


@pytest.mark.asyncio
async def test_drafting_remains_available_when_document_tools_are_recycled(tmp_path):
    store,runner=setup_runner(tmp_path)
    try:
        # A deterministic empty selection simulates admission recycling, not a model choice.
        empty={'operations':[], 'guidance':{}}
        await runner.assemble('task',word_tool_override=empty)
        assert 'word.plan_draft' in {t['function']['name'] for t in runner.native_tools}
        assert 'word.prepare_creation' not in {t['function']['name'] for t in runner.native_tools}
    finally:store.close()


@pytest.mark.asyncio
async def test_pre_call_rejection_is_visible_and_can_retry_in_focused_loop(tmp_path):
    class RetryProvider(DraftProvider):
        rejected=False
        async def stream(self,messages,*,tools,**kwargs):
            if self.calls==1 and not self.rejected:
                self.rejected=True
                yield {'type':'token','text':'Rejected malformed drafting output'}
                yield {'type':'finish','reason':'stop'}
                return
            if self.calls==1:
                assert 'REJECTED DRAFT OUTPUT OBSERVATION' in canonical(messages)
                assert 'Rejected malformed drafting output' in canonical(messages)
            async for event in super().stream(messages,tools=tools,**kwargs):yield event
    store,runner=setup_runner(tmp_path,RetryProvider())
    try:
        await runner.run('task','background')
        assert not [e for e in runner.events if e['type']=='error'],runner.events
        assert len([e for e in runner.events if e['type']=='tool_error'])==1
        assert draft_fragments.get(store,'task')['text']==DRAFT
        assert draft_budget.get(store,'task')==4
    finally:store.close()


@pytest.mark.asyncio
async def test_lazy_replanning_still_available_after_submission(tmp_path):
    store,runner=setup_runner(tmp_path)
    try:
        await runner.run('task','background')
        await runner.assemble('task',word_tool_override=build_state(['plan_draft']))
        names=[t['function']['name'] for t in runner.native_tools]
        assert names.count('word.plan_draft')==1
        tool=next(t for t in runner.native_tools if t['function']['name']=='word.plan_draft')
        assert tool['function']['parameters']['properties']['source_hash']=={'const':digest(OBJECTIVE.encode())}
        assert tool['function']['parameters']['properties']['source_range']['properties']['start']['enum']==['s01','s02','s03']
        assert 'word.submit_draft' not in names
    finally:store.close()


@pytest.mark.asyncio
async def test_non_opt_in_hides_draft_capability_even_if_saved_selection_exists(tmp_path):
    store,runner=setup_runner(tmp_path)
    try:
        ordinary=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True,requirements_planning=True)
        await ordinary.assemble('task',word_tool_override=build_state(['plan_draft']))
        assert 'word.plan_draft' not in [t['function']['name'] for t in ordinary.native_tools]
        loader=next(t for t in ordinary.native_tools if t['function']['name']=='word.load_tools')
        assert 'plan_draft' not in loader['function']['parameters']['properties']['operations']['items']['enum']
    finally:store.close()


def empty_selection(store):
    from localoffice.word.guidance import fingerprint
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical({
            'operations':[],'guidance':{},'source_hash':fingerprint([])})))


@pytest.mark.asyncio
async def test_isolated_planning_uses_same_loop_budget_and_full_source(tmp_path):
    class Planner(DraftProvider):
        async def stream(self,messages,*,tools,**kwargs):
            if self.calls==0:
                assert {t['function']['name'] for t in tools}=={'word.plan_draft','word.load_tools','ask_user'}
                for text in ('Keep the literal.', 'Draft an invitation to review.', 'Keep the ending.'):
                    assert text in canonical(messages)
                assert 'source_range' in canonical(tools)
            async for event in super().stream(messages,tools=tools,**kwargs):yield event
    store,runner=setup_runner(tmp_path,Planner(),isolated=True)
    empty_selection(store)
    try:
        await runner.run('task','background')
        assert not [e for e in runner.events if e['type']=='error'],runner.events
        assert draft_fragments.get(store,'task')['text']==DRAFT
        assert draft_budget.get(store,'task')==3
        assert store.db.execute("SELECT value FROM meta WHERE key='task_content_mode:task'").fetchone()[0].startswith('isolated_draft_planning_v1:')
    finally:store.close()


@pytest.mark.asyncio
async def test_isolated_planning_can_return_to_creation_without_drafting(tmp_path):
    class Skip(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            if self.calls==0:
                action={'name':'word.load_tools','arguments':{'operations':['prepare_creation']}}
            else:
                assert 'word.prepare_creation' in {t['function']['name'] for t in tools}
                assert OBJECTIVE in [m['content'] for m in messages if m['role']=='user']
                action={'name':'ask_user','arguments':{'text':'Scripted stop before document execution.'}}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    store,runner=setup_runner(tmp_path,Skip(None),isolated=True)
    empty_selection(store)
    try:
        await runner.run('task','background')
        assert not [e for e in runner.events if e['type']=='error'],runner.events
        assert draft_fragments.get(store,'task') is None
        assert draft_budget.get(store,'task')==2
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.asyncio
async def test_natural_requirements_to_isolated_plan_to_creation(tmp_path):
    class Natural(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            names={t['function']['name'] for t in tools}
            if self.calls==0:
                action={'name':'word.plan_requirements','arguments':{'source_hash':digest(OBJECTIVE.encode()),'requirements':[]}}
            elif self.calls==1:
                assert names=={'word.plan_draft','word.load_tools','ask_user'}
                assert OBJECTIVE not in [m['content'] for m in messages if m['role']=='user']
                action={'name':'word.plan_draft','arguments':{'source_hash':digest(OBJECTIVE.encode()),'source_range':{'start':'s02','end':'s02'}}}
            elif self.calls==2:
                tool=next(t for t in tools if t['function']['name']=='word.submit_draft')
                action={'name':'word.submit_draft','arguments':{'ref':tool['function']['parameters']['properties']['ref']['const'],'text':DRAFT}}
            else:
                assert 'word.prepare_creation' in names and 'word.plan_draft' not in names
                assert OBJECTIVE in [m['content'] for m in messages if m['role']=='user']
                action={'name':'ask_user','arguments':{'text':'Scripted stop before staging.'}}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    store=fixture(tmp_path,OBJECTIVE)
    runner=TaskRunner(store,Natural(None),source_planning=True,creation_review=True,requirements_planning=True,
                      focused_drafting=True,isolated_draft_planning=True)
    with store.transaction():
        store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(['plan_requirements']))))
    try:
        await runner.run('task','background')
        assert not [e for e in runner.events if e['type'] in ('error','tool_error')],runner.events
        assert draft_budget.get(store,'task')==4
        assert draft_fragments.get(store,'task')['text']==DRAFT
    finally:store.close()


@pytest.mark.asyncio
async def test_loading_planner_counts_its_actual_isolated_context(tmp_path):
    class Loader(NativeProvider):
        context_limit=4096
        async def count_tokens(self,messages,*,tools):
            if ('word.plan_draft' in {t['function']['name'] for t in tools}
                    and OBJECTIVE in [m['content'] for m in messages if m['role']=='user']):
                return 3500
            validate_history(messages)
            return 500
        async def stream(self,messages,*,tools,**kwargs):
            if self.calls==0:
                action={'name':'word.load_tools','arguments':{'operations':['plan_draft']}}
            else:
                assert {t['function']['name'] for t in tools}=={'word.plan_draft','word.load_tools','ask_user'}
                action={'name':'ask_user','arguments':{'text':'Scripted planner admission stop.'}}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    store,runner=setup_runner(tmp_path,Loader(None),isolated=True)
    try:
        await runner.run('task','background')
        assert not [e for e in runner.events if e['type'] in ('error','tool_error')],runner.events
        assert runner.provider.calls==2
        assert draft_budget.get(store,'task')==2
    finally:store.close()
