"""Opt-in requirement selection routes capabilities, never selects constraints."""
import json
import pytest
from localoffice.runtime import TaskRunner,CREATION_ONLY_OPERATIONS
from localoffice.storage import canonical,digest
from localoffice.errors import HarnessError
from localoffice.word.requirements import plan
from localoffice.word.guidance import build_state,selected
from test_word_content_plan import fixture
from test_native_runner import NativeProvider

OBJECTIVE='Use exact literal. Use one-inch margins.'
MODE='requirements_reviewed_source_planning'


class Scripted(NativeProvider):
    def __init__(self,actions):super().__init__(None);self.actions=actions;self.exposed=[]
    async def stream(self,messages,*,tools,**kwargs):
        self.exposed.append({t['function']['name'] for t in tools})
        action=self.actions[self.calls] if self.calls<len(self.actions) else {'name':'ask_user','arguments':{'text':'Scripted stop'}}
        self.calls+=1
        yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.parametrize('native,source,review',[(False,True,True),(True,False,True),(True,True,False)])
def test_mode_requires_native_source_and_review(tmp_path,native,source,review):
    store=fixture(tmp_path,OBJECTIVE);provider=NativeProvider(None);provider.native_tool_protocol=native
    try:
        with pytest.raises(HarnessError,match='requires native'):
            TaskRunner(store,provider,source_planning=source,creation_review=review,requirements_planning=True)
        assert not store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'task_content_mode:%'").fetchone()
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,fresh',[(False,False),(True,False),(True,True)])
async def test_source_success_routes_to_requirements_only_when_opted_in_and_needed(tmp_path,enabled,fresh):
    store=fixture(tmp_path,OBJECTIVE)
    if fresh:plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=[])
    provider=Scripted([{'name':'word.plan_content','arguments':{'literals':[{'role':'paragraph','quote':'exact literal'}]}}])
    runner=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=enabled)
    try:
        await runner.run('task','background')
        wanted='plan_requirements' if enabled and not fresh else 'prepare_creation'
        assert selected(store,'task')['operations']==[wanted]
        assert 'word.'+wanted in provider.exposed[1]
        assert store.db.execute("SELECT value FROM meta WHERE key='task_content_mode:task'").fetchone()[0]==(MODE if enabled else 'reviewed_source_planning')
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('entries',[[],[{'kind':'page_geometry','source_range':{'start':'s02','end':'s02'},'field':'margin','equals':1.0,'unit':'in'}]])
async def test_explicit_plan_including_empty_advances_to_prepare(tmp_path,entries):
    store=fixture(tmp_path,OBJECTIVE)
    provider=Scripted([{'name':'word.plan_requirements','arguments':{'source_hash':digest(OBJECTIVE.encode()),'requirements':entries}}])
    runner=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=True)
    try:
        await runner.run('task','background')
        assert selected(store,'task')['operations']==['prepare_creation']
        assert 'word.prepare_creation' in provider.exposed[1]
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['missing','stale','fresh','existing'])
async def test_missing_or_stale_plan_blocks_creation_exposure_but_keeps_sources(tmp_path,state):
    store=fixture(tmp_path,OBJECTIVE)
    if state in ('stale','fresh'):
        plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=[])
        if state=='stale':store.db.execute("UPDATE tasks SET objective=objective||' New request.' WHERE id='task'")
    if state=='existing':(store.root/'report.docx').write_bytes(b'External target')
    active=build_state(['prepare_creation','inspect','plan_content'])
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(active)))
    runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True,requirements_planning=True)
    try:
        await runner.assemble('task')
        names={t['function']['name'] for t in runner.native_tools}
        loader=next(t['function'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
        choices=set(loader['parameters']['properties']['operations']['items']['enum'])
        assert {'plan_content','repair_content_plan','inspect','plan_requirements'}<=choices
        assert {'word.plan_content','word.read_objects'}<=names
        if state in ('missing','stale'):
            assert 'word.plan_requirements' in names
            assert not choices&CREATION_ONLY_OPERATIONS
            assert not names&{'word.'+n for n in CREATION_ONLY_OPERATIONS}
        elif state=='fresh':assert 'word.prepare_creation' in names
        else:assert not names&{'word.'+n for n in CREATION_ONLY_OPERATIONS}
        assert json.loads(store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0])==active
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('previous,enabled',[(MODE,False),('reviewed_source_planning',True)])
async def test_resume_mode_mismatch_is_rejected_without_calls(tmp_path,previous,enabled):
    store=fixture(tmp_path,OBJECTIVE)
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('task_content_mode:task',previous))
    provider=Scripted([]);runner=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=enabled)
    try:
        await runner.run('task','background')
        assert provider.calls==0
        assert any(e.get('code')=='content_mode_change' for e in runner.events)
        assert store.db.execute("SELECT value FROM meta WHERE key='task_content_mode:task'").fetchone()[0]==previous
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('source_tool',['select_source_quotes','repair_content_plan'])
@pytest.mark.parametrize('fresh',[False,True])
async def test_other_source_successes_use_same_fresh_plan_routing(tmp_path,source_tool,fresh):
    from localoffice.word.source_quotes import candidates
    from localoffice.word.plugin import WordPlugin
    from localoffice.word.content_plan_repair import pending
    objective='Copy “Exact.” and draft an invitation. Use one-inch margins.'
    store=fixture(tmp_path,objective)
    if fresh:plan(store,'task',source_hash=digest(objective.encode()),requirements=[])
    if source_tool=='select_source_quotes':
        state=candidates(store,'task')
        action={'name':'word.select_source_quotes','arguments':{'source_hash':state['source_hash'],'selections':{'q01':'paragraph'}}}
    else:
        with pytest.raises(HarnessError):
            WordPlugin(store).plan_content(task_id='task',literals=[{'role':'paragraph','quote':'Exact.'},{'role':'paragraph','quote':'Invented invitation.'}])
        state=pending(store,'task')
        action={'name':'word.repair_content_plan','arguments':{'ref':state['ref'],'changes':[{'op':'defer_as_draft','index':1}]}}
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(['repair_content_plan']))))
    provider=Scripted([action]);runner=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=True)
    try:
        await runner.run('task','background')
        assert selected(store,'task')['operations']==['prepare_creation' if fresh else 'plan_requirements']
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()
