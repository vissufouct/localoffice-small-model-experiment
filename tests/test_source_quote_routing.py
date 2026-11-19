"""Native assembly routing with scripted tools; no inference or document worker."""
import json

import pytest

from localoffice.errors import HarnessError
from localoffice.native_tools import validate_history, validate_native_arguments
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore, digest
from localoffice.word.guidance import load
from test_native_objective_anchor import Provider
from test_word_content_plan import fixture


def functions(runner):
    return {tool['function']['name']:tool['function'] for tool in runner.native_tools}


@pytest.mark.asyncio
async def test_quoted_new_target_exposes_finite_source_hash_and_candidate_ids(tmp_path):
    objective='Copy " First café " and “Second 😀”; draft a conclusion.'
    store=fixture(tmp_path,objective)
    try:
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        messages=await runner.assemble('task');tools=functions(runner)
        assert runner.word_tool_state['operations']==['select_source_quotes']
        assert 'word.select_source_quotes' in tools and 'word.plan_content' not in tools
        assert 'word.prepare_creation' not in tools
        props=tools['word.select_source_quotes']['parameters']['properties']
        assert props['source_hash']=={'const':digest(objective.encode())}
        choices=props['selections']
        assert list(choices['properties'])==['q01','q02']
        assert choices['additionalProperties'] is False
        assert choices['minProperties']==1 and choices['maxProperties']==2
        for candidate in choices['properties'].values():
            assert candidate['enum']==['paragraph','header','footer','table_cell']
        line=next(line for line in messages[0]['content'].splitlines()
                  if line.startswith('Current exact quoted-source choices: '))
        navigation=json.loads(line.split(': ',1)[1])
        assert navigation['available'] and navigation['source_hash']==digest(objective.encode())
        assert navigation['candidates']['q01']['preview']==' First café '
        assert navigation['candidates']['q02']['preview']=='Second 😀'
        valid={'source_hash':navigation['source_hash'],'selections':{'q01':'header'}}
        validate_native_arguments('word.select_source_quotes',valid,runner.native_tools)
        for invalid in ({**valid,'source_hash':'0'*64},
                        {**valid,'selections':{'q03':'header'}},
                        {**valid,'selections':{'q01':'invented'}}):
            with pytest.raises(HarnessError):
                validate_native_arguments('word.select_source_quotes',invalid,runner.native_tools)
        assert messages[1]=={'role':'user','content':objective}
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('objective',['Create a report and draft the requested sentence.',
                                      'Copy "unfinished source passage'])
async def test_unavailable_quote_choices_leave_legacy_planning_available(tmp_path,objective):
    store=fixture(tmp_path,objective)
    try:
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        messages=await runner.assemble('task');tools=functions(runner)
        assert runner.word_tool_state['operations']==['plan_content']
        assert 'word.plan_content' in tools and 'word.select_source_quotes' not in tools
        assert 'Current exact quoted-source choices:' not in messages[0]['content']
    finally:store.close()


@pytest.mark.asyncio
async def test_explicit_legacy_planner_load_overrides_automatic_quote_routing(tmp_path):
    store=fixture(tmp_path,'Copy "Exact source" and draft a sentence.')
    try:
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        await runner.assemble('task')
        assert 'word.select_source_quotes' in functions(runner)
        with store.transaction():store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
        load(store,'task',['plan_content'])
        for _ in range(2):
            messages=await runner.assemble('task')
            assert runner.word_tool_state['operations']==['plan_content']
            assert 'word.plan_content' in functions(runner)
            assert 'word.select_source_quotes' not in functions(runner)
            assert 'Current exact quoted-source choices:' not in messages[0]['content']
    finally:store.close()


@pytest.mark.asyncio
async def test_successful_quote_selection_routes_to_prepare_without_candidate_navigation(tmp_path):
    objective='Copy "Exact body" with header “Exact header”; draft the conclusion.'
    store=ProjectStore(tmp_path)
    class ScriptedProvider(Provider):
        def __init__(self):super().__init__();self.calls=0;self.presented=[]

        def status(self):return {'loaded':True,'constrained_decoding':False}

        async def stream(self,messages,*,tools,**kwargs):
            validate_history(messages)
            self.presented.append(messages)
            names={tool['function']['name'] for tool in tools}
            if self.calls==0:
                assert 'word.select_source_quotes' in names
                action={'name':'word.select_source_quotes','arguments':{
                    'source_hash':digest(objective.encode()),
                    'selections':{'q01':'paragraph','q02':'header'}}}
            else:
                assert self.calls==1
                assert 'word.prepare_creation' in names
                assert 'word.select_source_quotes' not in names and 'word.plan_content' not in names
                assert 'Current exact quoted-source choices:' not in messages[0]['content']
                assert '"q01"' not in messages[0]['content'] and '"q02"' not in messages[0]['content']
                assert 'active_literal_plan' in messages[0]['content'] and 'ct_' in messages[0]['content']
                action={'name':'ask_user','arguments':{'text':'Scripted test ends before creation.'}}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=ScriptedProvider();runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        key=await runner.start(objective,artifact='report.docx',mode='background')
        await runner.job
        assert provider.calls==2,runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused',runner.events
        state=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',('word_tools:'+key,)).fetchone()[0])
        assert state['operations']==['prepare_creation']
        messages=await runner.assemble(key)
        assert 'Current exact quoted-source choices:' not in messages[0]['content']
        assert 'word.prepare_creation' in functions(runner)
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==2
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert store.candidate('report.docx') is None and not (tmp_path/'report.docx').exists()
    finally:
        await runner.pause();store.close()
