"""Quoted-source selection is lazy and delegates exact model choices to its owner."""
from pathlib import Path

import pytest

from localoffice.errors import HarnessError
from localoffice.plugins import first_party_registry
from localoffice.storage import ProjectStore
from localoffice.word import source_quotes
from localoffice.word.contracts import LoadTools
from localoffice.word.guidance import DIRECT_MODELS, MODELS, build_state, prompt_schemas
from localoffice.word.plugin import WordPlugin


def test_selector_schema_is_lazy_host_call_id_stays_private():
    assert MODELS['select_source_quotes'] is source_quotes.SourceQuoteSelection
    assert DIRECT_MODELS['select_source_quotes'] is source_quotes.SourceQuoteSelection
    assert LoadTools(operations=['select_source_quotes']).operations==['select_source_quotes']
    selected=prompt_schemas(['select_source_quotes'])
    assert set(selected)=={'word.select_source_quotes'}
    properties=selected['word.select_source_quotes']['properties']
    assert 'native_call_id' not in properties and 'task_id' not in properties
    assert {'source_hash','selections'}<=properties.keys()
    assert 'word.select_source_quotes' not in prompt_schemas(['plan_content'])
    assert 'word.stage_batch' not in prompt_schemas(['select_source_quotes','plan_content'])


def test_selector_guidance_preserves_fallback_and_model_choice():
    guide=build_state(['select_source_quotes'])['guidance']['select_source_quotes']
    assert 'model-chosen role' in guide
    assert 'q01..q24' in guide and 'creation uses returned ct_' in guide
    assert 'host assigns no roles' in guide
    assert 'exclude formatting instructions' in guide
    assert 'other user messages' in guide and 'load plan_content' in guide
    assert 'load prepare_creation or create' in guide
    assert 'does not certify complete intent coverage' in guide
    skill=Path('host/localoffice/builtin_skills/word-create/SKILL.md').read_text()
    tools=next(line for line in skill.splitlines() if line.startswith('allowed-tools:')).split()
    assert 'word.select_source_quotes' in tools and 'word.plan_content' in tools


@pytest.mark.asyncio
@pytest.mark.parametrize('call_id',[None,'native-call-for-selector'])
async def test_registered_selector_passes_exact_choices_and_host_identity(tmp_path,monkeypatch,call_id):
    store=ProjectStore(tmp_path);seen=[]
    choices={'source_hash':'a'*64,'selections':{'q02':'footer','q01':'paragraph'}}
    receipt={'status':'selected','document_changed':False}
    def select(s,t,**arguments):
        assert s is store and t=='task'
        assert arguments==dict(choices,native_call_id=call_id)
        seen.append(arguments)
        return receipt
    monkeypatch.setattr(source_quotes,'select',select)
    monkeypatch.setattr(WordPlugin,'_execute',lambda *a,**kw:pytest.fail('Unexpected document worker'))
    try:
        registry=first_party_registry(store)
        assert registry.tools['word.select_source_quotes']=='word'
        result=await registry.call('word.select_source_quotes',task_id='task',native_call_id=call_id,**choices)
        assert result is receipt and len(seen)==1
        assert choices['selections']=={'q02':'footer','q01':'paragraph'}
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        registry.set_enabled('word',False)
        with pytest.raises(HarnessError) as caught:
            registry.call('word.select_source_quotes',task_id='task',**choices)
        assert caught.value.code=='plugin_disabled' and len(seen)==1
    finally:store.close()


def test_selector_errors_propagate_without_rewriting_or_fallback(tmp_path,monkeypatch):
    store=ProjectStore(tmp_path);failure=HarnessError('content_source','Source changed')
    def select(*args,**kwargs):raise failure
    monkeypatch.setattr(source_quotes,'select',select)
    try:
        with pytest.raises(HarnessError) as caught:
            WordPlugin(store).select_source_quotes(task_id='task',source_hash='a'*64,selections={'q01':'header'})
        assert caught.value is failure
    finally:store.close()


@pytest.mark.asyncio
async def test_registered_selection_binds_only_chosen_quote_with_explicit_role(tmp_path):
    from test_word_content_plan import fixture
    from localoffice.word.content_plan import resolve,active_plan
    store=fixture(tmp_path,'Use "Report title" and "Page ". Draft an invitation.')
    try:
        registry=first_party_registry(store)
        await registry.call('word.load_tools',task_id='task',operations=['select_source_quotes'])
        choices=source_quotes.candidates(store,'task')
        assert choices['available'] and not choices['omitted']
        assert list(choices['candidates'])==['q01','q02']
        assert all('role' not in item for item in choices['candidates'].values())
        receipt=await registry.call('word.select_source_quotes',task_id='task',
            source_hash=choices['source_hash'],selections={'q02':'footer'})
        assert len(receipt['literals'])==1
        reference=receipt['literals'][0]['ref']
        assert reference.startswith('ct_')
        assert resolve(store,'task',reference,role='footer')=='Page '
        assert active_plan(store,'task')['refs']==[{'ref':reference,'role':'footer'}]
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective=? WHERE id='task'",('Use "Changed".',))
        with pytest.raises(HarnessError) as caught:
            await registry.call('word.select_source_quotes',task_id='task',
                source_hash=choices['source_hash'],selections={'q01':'header'})
        assert caught.value.code=='content_source_stale'
    finally:store.close()
