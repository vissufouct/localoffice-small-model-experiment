"""Explicit lazy draft planning is metadata, never a StageBatch edit."""
import copy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from localoffice.chats import current_chat
from localoffice.native_tools import action_tools, compact_tools, validate_native_arguments
from localoffice.runtime import WORD_ACTION
from localoffice.storage import ProjectStore
from localoffice.word import guidance
from localoffice.word.contracts import LoadTools, StageBatch
from localoffice.word.draft_fragments import PlanDraft
from localoffice.word.requirements import PlanRequirements


ACTION={'tool':'word.plan_draft','source_hash':'a'*64,
        'source_range':{'start':'s02','end':'s03'}}


def check_local_refs(schema):
    Draft202012Validator.check_schema(schema)

    def walk(value):
        if isinstance(value,dict):
            if '$ref' in value:
                assert value['$ref'].startswith('#/$defs/')
                assert value['$ref'].split('/')[-1] in schema['$defs']
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    walk(schema)


def test_loader_explicit_plan_only_and_existing_four_operation_cap():
    loader=LoadTools.model_json_schema()['properties']['operations']
    assert 'plan_draft' in loader['items']['enum']
    assert 'submit_draft' not in loader['items']['enum']
    assert loader['minItems']==1 and loader['maxItems']==4
    assert LoadTools(operations=['plan_draft']).operations==['plan_draft']
    LoadTools(operations=['plan_draft','plan_requirements','inspect','prepare_creation'])
    for names in ([],['submit_draft'],['plan_draft','plan_draft'],
                  ['plan_draft','plan_requirements','inspect','prepare_creation','create']):
        with pytest.raises(ValidationError):LoadTools(operations=names)
    assert guidance.MODELS['plan_draft'] is PlanDraft
    assert guidance.DIRECT_MODELS['plan_draft'] is PlanDraft
    assert 'submit_draft' not in guidance.MODELS
    assert 'submit_draft' not in guidance.DIRECT_MODELS
    assert 'submit_draft' not in guidance.GUIDES


@pytest.mark.parametrize('names', [[],['inspect'],['create'],['plan_requirements']])
def test_unselected_draft_tools_are_not_exposed(names):
    schema=guidance.project_schema(WORD_ACTION.json_schema(),names)
    assert 'PlanDraft' not in schema['$defs'] and 'SubmitDraft' not in schema['$defs']
    tools=compact_tools(action_tools(schema))
    names={tool['function']['name'] for tool in tools}
    assert 'word.plan_draft' not in names and 'word.submit_draft' not in names
    loader=next(tool['function']['parameters'] for tool in tools if tool['function']['name']=='word.load_tools')
    assert 'plan_draft' in loader['properties']['operations']['items']['enum']


@pytest.mark.parametrize('names', [['plan_draft'],['plan_draft','plan_requirements'],
    ['plan_draft','plan_content','select_source_quotes','inspect'],
    ['plan_draft','prepare_creation','finalize_creation','revise_creation']])
def test_metadata_only_selection_has_valid_direct_schema_without_batch(names):
    full=WORD_ACTION.json_schema()
    original=copy.deepcopy(full)
    projected=guidance.project_schema(full,names)
    assert full==original
    check_local_refs(projected)
    assert 'PlanDraft' in projected['$defs'] and 'SubmitDraft' not in projected['$defs']
    assert 'StageBatch' not in projected['$defs']
    Draft202012Validator(projected).validate(ACTION)
    assert isinstance(WORD_ACTION.validate_python(ACTION),PlanDraft)
    prompts=guidance.prompt_schemas(names)
    assert 'word.plan_draft' in prompts and 'word.stage_batch' not in prompts
    assert 'word.submit_draft' not in prompts
    for schema in prompts.values():check_local_refs(schema)
    Draft202012Validator(prompts['word.plan_draft']).validate(ACTION)
    tools=compact_tools(action_tools(projected))
    validate_native_arguments('word.plan_draft',{key:value for key,value in ACTION.items() if key!='tool'},tools)


@pytest.mark.parametrize('operation,model', [('format_table','FormatTable'),('set_list','SetList'),
                                          ('replace_text','ReplaceText')])
def test_mixed_metadata_and_document_edits_only_include_real_batch_operations(operation,model):
    names=['plan_draft',operation]
    projected=guidance.project_schema(WORD_ACTION.json_schema(),names)
    check_local_refs(projected)
    batch=projected['$defs']['StageBatch']
    assert batch['properties']['operations']['items']['oneOf']==[{'$ref':'#/$defs/'+model}]
    prompts=guidance.prompt_schemas(names)
    batch_prompt=prompts['word.stage_batch']
    check_local_refs(batch_prompt)
    assert batch_prompt['properties']['operations']['items']['oneOf']==[{'$ref':'#/$defs/'+model}]
    assert 'PlanDraft' not in batch_prompt['$defs']
    assert 'word.plan_draft' in prompts
    assert 'word.submit_draft' not in prompts


def test_guidance_loading_only_writes_selection_no_source_choice_or_generation(tmp_path):
    store=ProjectStore(tmp_path)
    try:
        with store.transaction():
            store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                             ('task','Draft requested prose. Finish afterwards.','running','background','report.docx',1,1))
            store.db.execute('INSERT INTO task_chats VALUES(?,?)',('task',current_chat(store)))
        before={row['key']:row['value'] for row in store.db.execute('SELECT * FROM meta')}
        receipt=guidance.load(store,'task',['plan_draft'])
        assert receipt=={'loaded_operations':['plan_draft']}
        state=guidance.selected(store,'task',new_target=True)
        assert state==guidance.build_state(['plan_draft'])
        assert state['guidance']=={'plan_draft':guidance.GUIDES['plan_draft']}
        after={row['key']:row['value'] for row in store.db.execute('SELECT * FROM meta')}
        assert set(after)-set(before)=={'word_tools:task'}
        assert all(after[key]==value for key,value in before.items())
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM native_calls').fetchone()[0]==0
        assert not (tmp_path/'report.docx').exists()
        guidance.load(store,'task',['inspect'])
        assert guidance.selected(store,'task')['operations']==['inspect']
    finally:store.close()


def test_existing_requirement_and_batch_count_caps_unchanged():
    assert PlanRequirements.model_json_schema()['properties']['requirements']['maxItems']==8
    assert StageBatch.model_json_schema()['properties']['operations']['maxItems']==24
    assert guidance.prompt_schemas(['plan_requirements'])['word.plan_requirements']['properties']['requirements']['maxItems']==8
    batch=guidance.prompt_schemas(['plan_draft','set_list'])['word.stage_batch']
    assert batch['properties']['operations']['maxItems']==24
