"""Lazy host registration and rollback-before-capture; no document worker needed."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from localoffice.errors import HarnessError
from localoffice.plugins import first_party_registry
from localoffice.runtime import WORD_ACTION
from localoffice.storage import canonical
from localoffice.word import content_plan, content_plan_repair
from localoffice.word.contracts import LoadTools
from localoffice.word.guidance import build_state, project_schema, prompt_schemas
from localoffice.word.plugin import WordPlugin
from test_word_content_plan import fixture


@pytest.mark.asyncio
async def test_lazy_registration_and_creation_skill(tmp_path):
    store=fixture(tmp_path)
    try:
        registry=first_party_registry(store)
        assert registry.tools['word.repair_content_plan']=='word'
        assert LoadTools(operations=['repair_content_plan']).operations==['repair_content_plan']
        state=await registry.call('word.load_tools',task_id='task',operations=['repair_content_plan'])
        assert 'repair_content_plan' in canonical(state)
        guide=build_state(['repair_content_plan'])['guidance']['repair_content_plan']
        assert 'retain unchanged selections' in guide
        assert 'For requested drafting, use defer_as_draft' in guide
        assert 'not the requested content' in guide
        assert 'Do not ask users to approve already-requested drafting' in guide
        assert set(prompt_schemas(['repair_content_plan']))=={'word.repair_content_plan'}
        projected=project_schema(WORD_ACTION.json_schema(),['repair_content_plan'])
        assert 'RepairContentPlan' in projected['$defs']
        assert 'StageBatch' not in projected['$defs']
        assert 'RepairContentPlan' not in project_schema(WORD_ACTION.json_schema(),['plan_content'])['$defs']
        skill=Path('host/localoffice/builtin_skills/word-create/SKILL.md').read_text()
        assert 'word.repair_content_plan' in next(line for line in skill.splitlines() if line.startswith('allowed-tools:'))
        registry.set_enabled('word',False)
        with pytest.raises(HarnessError) as caught:
            registry.call('word.repair_content_plan',task_id='task')
        assert caught.value.code=='plugin_disabled'
    finally:store.close()


@pytest.mark.asyncio
async def test_real_capture_after_rollback_and_explicit_registered_repair(tmp_path,monkeypatch):
    store=fixture(tmp_path);original=content_plan_repair.capture;seen=[]
    def capture(store,task_id,arguments,error,**kwargs):
        assert not store.db.in_transaction
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        assert content_plan.active_plan(store,task_id) is None
        seen.append(error)
        return original(store,task_id,arguments,error,**kwargs)
    monkeypatch.setattr(content_plan_repair,'capture',capture)
    monkeypatch.setattr(WordPlugin,'_execute',lambda *a,**kw:pytest.fail('Unexpected worker'))
    try:
        registry=first_party_registry(store)
        with pytest.raises(HarnessError) as caught:
            await registry.call('word.plan_content',task_id='task',literals=[
                {'role':'paragraph','quote':'First page'},
                {'role':'footer','quote':'Second page','occurrence':4}])
        assert caught.value.__cause__ is seen[0] and caught.value.code=='content_quote'
        saved=content_plan_repair.failure_result(store,'task',{'literals':[
            {'role':'paragraph','quote':'First page'},
            {'role':'footer','quote':'Second page','occurrence':4}]})
        assert {'code':caught.value.code,'message':str(caught.value),'recovery':caught.value.recovery}==saved
        summary=caught.value.recovery['plan_repair']
        assert summary['status']=='pending' and summary['selection_count']==2
        assert summary['ref'].startswith('cp_')
        assert summary==content_plan_repair.pending(store,'task')
        assert len(canonical(caught.value.recovery).encode())<=2048
        arguments={'ref':summary['ref'],'changes':[{'op':'replace','index':1,
                    'selection':{'role':'footer','quote':'Second page','occurrence':0}}]}
        receipt=await registry.call('word.repair_content_plan',task_id='task',**arguments)
        assert receipt['status']=='repaired' and receipt['document_changed'] is False
        assert [item['role'] for item in receipt['references']]==['paragraph','footer']
        assert await registry.call('word.repair_content_plan',task_id='task',**arguments)==receipt
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('code',['content_quote','content_ambiguous'])
def test_large_candidate_prioritizes_consistent_handle_preserves_error(tmp_path,monkeypatch,code):
    store=fixture(tmp_path)
    recovery={'candidate_call':{'huge':'漢'*2000},'candidate_only':True,'invalid':[{'index':0}]}
    error=HarnessError(code,'Original exact-match diagnostic.',recovery=recovery)
    hint={'status':'pending','ref':'cp_'+'a'*32,'selection_count':1}
    def fail(*a,**kw):raise error
    def capture(s,t,args,exc,**kwargs):
        assert exc.recovery is recovery
        assert args['literals'][0]['quote']=='First page'
        return hint
    monkeypatch.setattr(content_plan,'plan',fail)
    monkeypatch.setattr(content_plan_repair,'capture',capture)
    failure={'code':code,'message':'Original exact-match diagnostic.', 'recovery':{'plan_repair':hint}}
    monkeypatch.setattr(content_plan_repair,'failure_result',lambda *a,**kw:failure)
    try:
        with pytest.raises(HarnessError) as caught:
            WordPlugin(store).plan_content(task_id='task',literals=[{'role':'header','quote':'First page'}])
        assert caught.value.__cause__ is error
        assert str(caught.value)==failure['message']
        assert caught.value.recovery==failure['recovery']
        assert 'candidate_call' in recovery
        assert len(canonical(caught.value.recovery).encode())<=2048
    finally:store.close()


def test_shape_errors_other_errors_and_success_do_not_capture(tmp_path,monkeypatch):
    store=fixture(tmp_path)
    monkeypatch.setattr(content_plan_repair,'capture',lambda *a:pytest.fail('Unexpected capture'))
    try:
        plugin=WordPlugin(store)
        with pytest.raises(ValidationError):plugin.plan_content(task_id='task',literals=[])
        result=plugin.plan_content(task_id='task',literals=[{'role':'header','quote':'First page'}])
        assert result['literals']
        error=HarnessError('chat_scope','Wrong owner')
        def fail(*a,**kw):raise error
        monkeypatch.setattr(content_plan,'plan',fail)
        with pytest.raises(HarnessError) as caught:
            plugin.plan_content(task_id='task',literals=[{'role':'header','quote':'First page'}])
        assert caught.value is error
    finally:store.close()


def test_repair_delegates_exact_arguments_without_inferred_changes(tmp_path,monkeypatch):
    store=fixture(tmp_path);args={'ref':'cp_'+'b'*32,'changes':[{'op':'defer_as_draft','index':0}]}
    expected={'status':'repaired'}
    def repair(s,t,**kwargs):
        assert s is store and t=='task' and kwargs==args
        return expected
    monkeypatch.setattr(content_plan_repair,'repair',repair)
    try:assert WordPlugin(store).repair_content_plan(task_id='task',**args) is expected
    finally:store.close()
