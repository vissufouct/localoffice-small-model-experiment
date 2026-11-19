import json
import pytest
from localoffice.runtime import WORD_ACTION,TaskRunner
from localoffice.word.guidance import project_schema,selected,load,GUIDES
from localoffice.word.contracts import LoadTools
from localoffice.storage import canonical,ProjectStore
from localoffice.errors import HarnessError
from pydantic import ValidationError
from test_word_progress import task
from test_word_tables import saved_table


def test_schema_projection_omits_unselected_definitions_and_keeps_references():
    full=WORD_ACTION.json_schema();original=canonical(full)
    initial=project_schema(full,[])
    assert 'StageBatch' not in initial['$defs'] and 'CreateDocument' not in initial['$defs']
    assert 'LoadTools' in initial['$defs']
    narrowed=project_schema(full,['set_list'])
    assert 'SetList' in narrowed['$defs'] and 'FormatTable' not in narrowed['$defs'] and 'ReplaceText' not in narrowed['$defs']
    assert narrowed['$defs']['StageBatch']['properties']['operations']['items']['discriminator']['mapping']=={'set_list':'#/$defs/SetList'}
    assert len(canonical(narrowed))<len(original)/2
    for names in [[],['create'],['format_table','set_cell_text'],['replace_text','set_list','insert_rows','set_style']]:
        schema=project_schema(full,names)
        def walk(v):
            if isinstance(v,dict):
                if '$ref' in v:assert v['$ref'].split('/')[-1] in schema['$defs']
                for child in v.values():walk(child)
            elif isinstance(v,list):
                for child in v:walk(child)
        walk(schema)
    assert canonical(full)==original


def test_selected_guidance_replaces_persists_and_invalidates_on_change(tmp_path,monkeypatch):
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    result=load(store,'task',['set_list'])
    state=selected(store,'task');assert len(state['source_hash'])==64 and result['loaded_operations']==state['operations']
    assert 'source_hash' not in result
    assert list(state['guidance'])==['set_list']
    assert selected(store,'other')['operations']==[]
    assert selected(store,'new',new_target=True)['operations']==['create']
    store.close();store=ProjectStore(tmp_path)
    try:
        assert selected(store,'task')==state
        with store.transaction():store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
        load(store,'task',['format_table'])
        assert selected(store,'task')['operations']==['format_table']
        monkeypatch.setitem(GUIDES,'format_table','Updated implementation guidance')
        assert selected(store,'task')['operations']==[] and selected(store,'task')['invalidated']
        with pytest.raises(HarnessError):load(store,'missing',['set_list'])
    finally:store.close()


@pytest.mark.parametrize('names',[[],['set_list']*2,['bad'],['replace_text','set_style','set_list','insert_rows','format_table']])
def test_loaded_set_is_bounded(names):
    with pytest.raises(ValidationError):LoadTools(operations=names)


@pytest.mark.asyncio
@pytest.mark.parametrize('constrained',[False,True])
async def test_runner_loads_selected_help_and_schema_before_next_model_call(tmp_path,constrained):
    class Provider:
        context_limit=4096
        def __init__(self):self.calls=[]
        def status(self):return {'loaded':True,'constrained_decoding':constrained}
        async def count_tokens(self,messages):return sum(len(m['content'])//4 for m in messages)
        async def stream(self,messages,**kwargs):
            self.calls.append((messages,kwargs['schema']))
            action={'tool':'word.load_tools','operations':['set_list']} if len(self.calls)==1 else {'tool':'ask_user','text':'Ready for targeted list work.'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    store,plugin,original,revision=saved_table(tmp_path);provider=Provider();runner=TaskRunner(store,provider)
    try:
        key=await runner.start('Inspect list settings',artifact='report.docx');await runner.job
        assert len(provider.calls)==2
        first,second=provider.calls
        assert 'SetList' not in first[1]['$defs'] and 'SetList' in second[1]['$defs']
        assert 'FormatTable' not in second[1]['$defs']
        assert ('Selected operation JSON schemas' in second[0][0]['content']) is (not constrained)
        assert GUIDES['set_list'] in second[0][0]['content']
        assert GUIDES['format_table'] not in second[0][0]['content']
        assert selected(store,key)['operations']==['set_list']
        assert runner.context_info['word_tools']['operations']==['set_list']
        assert store.candidate('report.docx') is None
        runner.registry.set_enabled('word',False)
        with pytest.raises(HarnessError,match='disabled'):
            await runner.registry.call('word.load_tools',task_id=key,operations=['format_table'])
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_oversized_help_selection_does_not_replace_active_context(tmp_path):
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True,'constrained_decoding':False}
        async def count_tokens(self,messages):return sum(len(m['content'])//4 for m in messages)
        async def stream(self,messages,**kwargs):
            actions=[{'tool':'word.load_tools','operations':['create','format_table','format_paragraph','set_section_layout']},
                     {'tool':'word.load_tools','operations':['set_list']},
                     {'tool':'ask_user','text':'Ready for list work.'}]
            action=actions[self.step];self.step+=1
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    store,plugin,original,revision=saved_table(tmp_path);runner=TaskRunner(store,Provider())
    try:
        key=await runner.start('Inspect list settings',artifact='report.docx');await runner.job
        errors=[e for e in runner.events if e['type']=='tool_error']
        assert len(errors)==1 and errors[0]['code']=='context_capacity'
        assert selected(store,key)['operations']==['set_list']
        assert not store.candidate('report.docx')
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[False,True,'create_character_style','set_character_style'])
async def test_unknown_locator_returns_scoped_discovery_recovery(tmp_path,mutation):
    store,plugin,original,revision=saved_table(tmp_path)
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 400
        async def stream(self,messages,**kwargs):
            actions=[{'tool':'word.read_objects','path':'report.docx','revision':revision,'ids':['paragraph:999']},
                     {'tool':'word.list_objects','path':'report.docx','revision':revision,'kind':'paragraph'},
                     {'tool':'ask_user','text':'Ready'}]
            action=actions[self.step]
            if mutation and self.step==0:action={'tool':'word.stage_batch','path':'report.docx','revision':revision,'operations':[{'id':'bad','op':'set_list','target':'paragraph ID from read','expected_hash':'0'*64,'number_id':2,'level':0}]}
            if isinstance(mutation,str) and self.step==0:
                operation={'id':'bad','op':mutation,'target':'guessed locator','expected_hash':'0'*64,'style_id':'Callout'}
                operation.update({'name':'Callout','inherit_base':False} if mutation=='create_character_style' else {'start':0,'end':3})
                action={'tool':'word.stage_batch','path':'report.docx','revision':revision,'operations':[operation]}
            self.step+=1
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        await runner.start('Read paragraph',artifact='report.docx');await runner.job
        error=next(e for e in runner.events if e['type']=='tool_error')
        assert error['code']=='unknown_object'
        assert error['recovery']=={'tool':'word.list_objects','path':'report.docx','revision':revision,'kind':'style' if mutation=='create_character_style' else 'paragraph','limit':20}
        assert not store.candidate('report.docx')
    finally:await runner.pause();store.close()


def test_prompt_schema_includes_complete_tool_envelope_and_only_selected_edits():
    from localoffice.word.guidance import prompt_schemas
    schemas=prompt_schemas(['set_list'])
    batch=schemas['word.stage_batch']
    assert set(batch['required'])>={'tool','path','revision','operations'}
    assert batch['properties']['tool']['const']=='word.stage_batch'
    assert 'SetList' in batch['$defs'] and 'FormatTable' not in batch['$defs']
    assert 'word.create' not in schemas
    projected=project_schema(WORD_ACTION.json_schema(),['set_list'])
    for ref in projected['oneOf']:assert 'tool' in projected['$defs'][ref['$ref'].split('/')[-1]]['required']


def test_typography_only_schema_excludes_layout_capabilities():
    schema=project_schema(WORD_ACTION.json_schema(),['format_style_typography'])
    assert 'FormatStyleTypography' in schema['$defs']
    assert 'ParagraphFormat' not in schema['$defs'] and 'FormatStyle' not in schema['$defs']
    assert set(schema['$defs']['Typography']['properties'])=={'font','size_points','size_half_points','color','bold','italic'}


def test_image_operations_load_bounded_asset_dependency_and_invalidate_old_selection(tmp_path):
    from localoffice.word.guidance import build_state,fingerprint
    for name in ('insert_image','replace_image'):
        state=build_state([name])
        assert state['operations']==[name,'import_image']
        schema=project_schema(WORD_ACTION.json_schema(),state['operations'])
        assert 'ImportImage' in schema['$defs'] and 'CreateDocument' not in schema['$defs']
        assert build_state([name,'import_image'])['operations']==[name,'import_image']
    with pytest.raises(HarnessError,match='at most three'):
        build_state(['replace_image','format_image','format_table','set_style'])
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    try:
        old={'operations':['replace_image'],'source_hash':fingerprint(['replace_image']),'guidance':{'replace_image':GUIDES['replace_image']}}
        with store.transaction():store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(old)))
        assert selected(store,'task')['invalidated']
        result=load(store,'task',['replace_image'])
        assert result['loaded_operations']==['replace_image','import_image']
        assert selected(store,'task')['operations']==result['loaded_operations']
    finally:store.close()


@pytest.mark.asyncio
async def test_help_admission_reserves_next_tool_result(tmp_path):
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):
            return 2500 if '"operations":["format_image","set_style"]' in messages[0]['content'] else 500
        async def stream(self,messages,**kwargs):
            self.step+=1
            action={'tool':'word.load_tools','operations':['format_image','set_style']} if self.step==1 else {'tool':'ask_user','text':'Selection needs more capacity.'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    store,plugin,original,rev=saved_table(tmp_path);runner=TaskRunner(store,Provider())
    try:
        key=await runner.start('Inspect image',artifact='report.docx');await runner.job
        errors=[e for e in runner.events if e['type']=='tool_error']
        assert len(errors)==1 and errors[0]['code']=='context_capacity'
        assert selected(store,key)['operations']==[]
        assert store.candidate('report.docx') is None
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_new_target_guidance_switches_to_editing_after_staging(tmp_path):
    from localoffice.word.guidance import BASE, CREATION_BASE
    from test_word_page_bands import band_design
    class Provider:
        context_limit=32768
        def __init__(self):self.prompts=[]
        def status(self):return {'loaded':True,'constrained_decoding':False}
        async def count_tokens(self,messages):return sum(len(m['content'])//4 for m in messages)
        async def stream(self,messages,**kwargs):
            self.prompts.append(messages[0]['content'])
            action={'tool':'word.create',**band_design()} if len(self.prompts)==1 else {'tool':'finish','text':'Staged'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    store=ProjectStore(tmp_path);provider=Provider();runner=TaskRunner(store,provider)
    try:
        objective='Create the complete report, preserving all supplied text.'
        key=await runner.start(objective,artifact='report.docx',mode='background');await runner.job
        assert len(provider.prompts)==2
        first,second=provider.prompts
        assert first.startswith(CREATION_BASE) and second.startswith(BASE)
        assert len(CREATION_BASE)<len(BASE)/2
        assert 'Catalogue: ' not in first and 'Catalogue: ' in second
        for prompt in provider.prompts:
            assert objective in prompt and 'Pinned task skill guidance' in prompt
            assert 'network_policy' in prompt and 'edit_scope' in prompt
        candidate=store.candidate('report.docx');assert candidate
        assert candidate['candidate_hash'] in second and not (tmp_path/'report.docx').exists()
        resumed=await runner.assemble(key)
        assert resumed[0]['content'].startswith(BASE)
    finally:await runner.pause();store.close()


def test_inspect_adds_no_schema_to_existing_edit_interface():
    full=WORD_ACTION.json_schema()
    assert project_schema(full,['replace_text'])==project_schema(full,['inspect','replace_text'])


@pytest.mark.asyncio
@pytest.mark.parametrize('operations,admitted',[
    (['inspect','replace_text'],True),
    (['replace_text','format_text'],False),
])
async def test_inspect_does_not_consume_multi_edit_receipt_reserve(tmp_path,operations,admitted):
    class Provider:
        native_tool_protocol=True
        context_limit=4096
        calls=0
        def status(self):return {'loaded':True,'constrained_decoding':False}
        async def count_tokens(self,messages,*,tools):return 2700
        async def stream(self,messages,**kwargs):
            action={'name':'word.load_tools','arguments':{'operations':operations}} if self.calls==0 else {'name':'ask_user','arguments':{'text':'Review the loaded capability.'}}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    store,plugin,original,revision=saved_table(tmp_path);runner=TaskRunner(store,Provider())
    try:
        key=await runner.start('Inspect the document before editing.',artifact='report.docx');await runner.job
        errors=[e for e in runner.events if e['type']=='tool_error']
        assert bool(errors) is (not admitted)
        if errors:assert errors[0]['code']=='context_capacity'
        assert selected(store,key)['operations']==(operations if admitted else [])
        assert store.candidate('report.docx') is None
    finally:await runner.pause();store.close()
