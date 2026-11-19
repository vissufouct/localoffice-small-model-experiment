"""Lazy creation-requirements discovery and exact-call metadata recovery."""
import json
import re

import pytest

from localoffice.runtime import TaskRunner,WORD_ACTION,CREATION_ONLY_OPERATIONS
from localoffice.storage import canonical,digest
from localoffice.plugins import first_party_registry
from localoffice.native_history import record_call,reconcile_word_call
from localoffice.native_tools import validate_native_arguments
from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError
from localoffice.word.guidance import build_state,prompt_schemas
from localoffice.word.requirements import plan,get,RESULT_PREFIX
from test_word_content_plan import fixture
from test_native_runner import NativeProvider

OBJECTIVE='Use one-inch margins. Draft 8–16 words.'
REQUIREMENTS_MODES=('requirements_reviewed_source_planning',
                    'focused_drafting_v1:requirements_reviewed_source_planning',
                    'isolated_draft_planning_v1:focused_drafting_v1:requirements_reviewed_source_planning')


def arguments():
    return {'source_hash':digest(OBJECTIVE.encode()),'requirements':[{
        'kind':'page_geometry','source_quote':'one-inch margins','field':'margin','equals':1.0,'unit':'in'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('existing',[False,True])
async def test_lazy_discovery_and_exact_hash_native_grammar(tmp_path,existing):
    store=fixture(tmp_path,OBJECTIVE);runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
    try:
        if existing:(store.root/'report.docx').write_bytes(b'Existing target, no document operations')
        await runner.assemble('task')
        names={t['function']['name'] for t in runner.native_tools}
        assert 'word.plan_requirements' not in names
        loader=next(t['function'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
        assert 'plan_requirements' in loader['parameters']['properties']['operations']['items']['enum']
        assert 'plan_requirements' not in CREATION_ONLY_OPERATIONS
        state=build_state(['plan_requirements'])
        assert set(prompt_schemas(['plan_requirements']))=={'word.plan_requirements'}
        with store.transaction():store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(state)))
        messages=await runner.assemble('task')
        assert runner.word_tool_state['operations']==['plan_requirements']
        tool=next(t for t in runner.native_tools if t['function']['name']=='word.plan_requirements')
        assert tool['function']['parameters']['properties']['source_hash']=={'const':arguments()['source_hash']}
        assert 'task_id' not in tool['function']['parameters']['properties']
        assert 'native_call_id' not in tool['function']['parameters']['properties']
        actual=[tool,next(t for t in runner.native_tools if t['function']['name']=='ask_user')]
        regex=native_tool_regex(actual)
        for source_hash,expected in [(arguments()['source_hash'],True),('a'*64,False)]:
            args={'source_hash':source_hash,'requirements':[{'kind':'page_geometry',
                'source_range':{'start':'s01','end':'s01'},'field':'margin','equals':1.0,'unit':'in'}]}
            raw='<tool_call>'+json.dumps({'name':'word.plan_requirements','arguments':args},ensure_ascii=False,separators=(',',':'))+'</tool_call>'
            assert bool(re.fullmatch(regex,raw))==expected
            if expected:validate_native_arguments('word.plan_requirements',args,runner.native_tools)
            else:
                with pytest.raises(HarnessError):validate_native_arguments('word.plan_requirements',args,runner.native_tools)
        assert sum(m.get('content')==OBJECTIVE for m in messages if m['role']=='user')==1
        assert get(store,'task') is None
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_registry_and_recovery_require_exact_execution_without_reactivating_old_plan(tmp_path):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        args=arguments();action={'tool':'word.plan_requirements',**args}
        WORD_ACTION.validate_python(action)
        identity=record_call(store,'task',action)
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        registry=first_party_registry(store)
        original=await registry.call('word.plan_requirements',task_id='task',native_call_id=identity,**args)
        replacement=plan(store,'task',source_hash=args['source_hash'],requirements=[])
        assert replacement['generation']>original['generation']
        active=get(store,'task');meta=[tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key LIKE 'word_requirements%' OR key LIKE 'word_tools:%' ORDER BY key")]
        recovered=reconcile_word_call(store,'task')
        assert recovered['state']=='reconciled'
        assert json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(recovered['message_id'],)).fetchone()[0])==original
        assert get(store,'task')==active
        assert [tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key LIKE 'word_requirements%' OR key LIKE 'word_tools:%' ORDER BY key")]==meta
        another=record_call(store,'task',action)
        assert another!=identity
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def test_tampered_requirements_receipt_fails_closed(tmp_path):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        args=arguments();identity=record_call(store,'task',{'tool':'word.plan_requirements',**args})
        plan(store,'task',**args,native_call_id=identity)
        key=RESULT_PREFIX+identity
        data=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
        data['state']['receipt']['count']=99
        store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(data),key))
        with pytest.raises(HarnessError,match='integrity'):reconcile_word_call(store,'task')
        assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(identity,)).fetchone()[0] is None
    finally:store.close()


@pytest.mark.parametrize('mode',REQUIREMENTS_MODES)
@pytest.mark.parametrize('condition',['fresh','missing_selection','stale','superseded',
                                    'disk','candidate','newer_selection','newer_same_load','unexecuted'])
def test_interrupted_requirements_recovery_mode_and_existing_guards(tmp_path,mode,condition):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        selected=canonical(build_state(['plan_requirements']))
        with store.transaction():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('task_content_mode:task',mode))
            if condition!='missing_selection':
                store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',selected))
        args=arguments()
        identity=record_call(store,'task',{'tool':'word.plan_requirements',**args})
        receipt=None if condition=='unexecuted' else plan(store,'task',**args,native_call_id=identity)
        if condition=='stale':
            with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed objective' WHERE id='task'")
        elif condition=='superseded':plan(store,'task',source_hash=args['source_hash'],requirements=[])
        elif condition=='disk':(store.root/'report.docx').write_bytes(b'Existing target')
        elif condition=='candidate':
            with store.transaction():
                store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',('report.docx','op',None,'a'*64))
        elif condition=='newer_selection':
            with store.transaction():
                store.db.execute('UPDATE meta SET value=? WHERE key=?',
                                 (canonical(build_state(['inspect'])),'word_tools:task'))
        elif condition=='newer_same_load':
            from localoffice.word.guidance import load
            loaded=load(store,'task',['plan_requirements'])
            store.message('tool',canonical(loaded),'task')
        before=[tuple(row) for row in store.db.execute('SELECT * FROM meta ORDER BY key')]
        active=get(store,'task')
        recovered=reconcile_word_call(store,'task')
        assert get(store,'task')==active
        if condition=='unexecuted':
            assert recovered['state']=='unresolved'
            assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(identity,)).fetchone()[0] is None
            assert [tuple(row) for row in store.db.execute('SELECT * FROM meta ORDER BY key')]==before
        else:
            assert recovered['state']=='reconciled'
            assert recovered['routing_restored'] is (condition in ('fresh','missing_selection'))
            assert json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',
                                               (recovered['message_id'],)).fetchone()[0])==receipt
            current=store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()
            if condition in ('fresh','missing_selection'):
                expected='plan_draft' if mode.startswith('isolated_draft_planning_v1:') else 'prepare_creation'
                assert current[0]==canonical(build_state([expected]))
            else:
                assert current[0]==dict(before)['word_tools:task']
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('mode',[None,'direct','reviewed_source_planning','unknown',
    'focused_drafting_v1:reviewed_source_planning',
    'focused_drafting_v2:requirements_reviewed_source_planning',
    'focused_drafting_v1:requirements_reviewed_source_planning:unknown'])
def test_unknown_or_incompatible_modes_recover_receipt_without_transition(tmp_path,mode):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        selected=canonical(build_state(['plan_requirements']))
        with store.transaction():
            store.db.execute('DELETE FROM meta WHERE key=?',('task_content_mode:task',))
            if mode is not None:
                store.db.execute('INSERT INTO meta VALUES(?,?)',('task_content_mode:task',mode))
            store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',selected))
        args=arguments()
        identity=record_call(store,'task',{'tool':'word.plan_requirements',**args})
        receipt=plan(store,'task',**args,native_call_id=identity)
        active=get(store,'task')
        recovered=reconcile_word_call(store,'task')
        assert recovered['state']=='reconciled' and recovered['routing_restored'] is False
        assert store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0]==selected
        assert get(store,'task')==active
        assert json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',
                                           (recovered['message_id'],)).fetchone()[0])==receipt
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.asyncio
async def test_runtime_dispatch_passes_host_call_identity_without_automatic_routing(tmp_path):
    store=fixture(tmp_path,OBJECTIVE)
    class Scripted(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            self.calls+=1
            native_args=arguments()
            native_args['requirements'][0].pop('source_quote')
            native_args['requirements'][0]['source_range']={'start':'s01','end':'s01'}
            action=({'name':'word.plan_requirements','arguments':native_args} if self.calls==1 else
                    {'name':'ask_user','arguments':{'text':'Scripted stop'}})
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Scripted(None),source_planning=True,creation_review=True)
    try:
        state=build_state(['plan_requirements'])
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(state)))
        await runner.run('task','background')
        assert get(store,'task')['generation']==1
        row=store.db.execute("SELECT call_id FROM native_calls WHERE function LIKE '%word.plan_requirements%'").fetchone()
        assert store.db.execute('SELECT 1 FROM meta WHERE key=?',(RESULT_PREFIX+row[0],)).fetchone()
        assert json.loads(store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0])==state
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()
