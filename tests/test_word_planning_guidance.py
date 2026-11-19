"""Copied-only guidance reaches native schemas without narrowing source semantics."""
import copy

from localoffice.native_tools import action_tools, compact_tools, validate_native_arguments
from localoffice.runtime import WORD_ACTION
from localoffice.word.content_plan import PlanContent
from localoffice.word.guidance import GUIDES, build_state, project_schema


def test_copied_only_scope_and_prefix_caution_reach_active_guidance():
    guide=build_state(['plan_content'])['guidance']['plan_content']
    assert guide==GUIDES['plan_content']
    assert guide.startswith('Plan COPIED passages ONLY:')
    assert 'Never put newly drafted content in selections.' in guide
    assert 'generate it later as plain creation text' in guide
    assert 'never shorten requested drafting into a matching source prefix' in guide
    assert 'preserve uncertain requested literals' in guide
    assert 'Select each requested paragraph' not in guide
    assert len(guide.encode())<=1200


def test_native_quote_description_retained_and_other_schema_facts_unchanged():
    full=WORD_ACTION.json_schema();original=copy.deepcopy(full)
    projected=project_schema(full,['plan_content'])
    tools=compact_tools(action_tools(projected))
    tool=next(item for item in tools if item['function']['name']=='word.plan_content')
    # Description must survive both native schema projection and compaction.
    parameters=tool['function']['parameters']
    quote=parameters['$defs']['LiteralSelection']['properties']['quote']
    assert 'COPIED source passages ONLY' in quote['description']
    assert 'Never newly drafted content' in quote['description']
    assert 'plain creation text' in quote['description']
    assert len(quote['description'].encode())<=220
    assert quote['minLength']==1 and quote['maxLength']==8000
    assert parameters['properties']['literals']['minItems']==1
    assert parameters['properties']['literals']['maxItems']==24
    assert parameters['$defs']['LiteralSelection']['properties']['role']['enum']==[
        'paragraph','header','footer','table_cell']
    assert full==original
    # This is guidance, not a prose classifier or a new validation restriction.
    arguments={'literals':[{'role':'paragraph','quote':'Newly drafted-looking prose.'}],
               'plan_mode':'replace','source_message':1}
    validate_native_arguments('word.plan_content',arguments,tools)
    assert PlanContent(**arguments).literals[0].quote==arguments['literals'][0]['quote']


def test_unselected_planning_descriptions_do_not_expand_other_native_tools():
    schema=project_schema(WORD_ACTION.json_schema(),['prepare_creation'])
    assert 'PlanContent' not in schema['$defs']
    assert 'LiteralSelection' not in schema['$defs']
