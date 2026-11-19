"""Native four-change batching is explicit; full host revision remains available."""
import copy
import json
import re

import pytest

from localoffice.decoding import native_tool_regex,structural_schema
from localoffice.errors import HarnessError
from localoffice.native_tools import action_tools,compact_tools,creation_state_tools,validate_native_arguments,creation_revision_path_pattern
from localoffice.runtime import WORD_ACTION
from localoffice.word.contracts import ReviseCreation
from localoffice.word.guidance import project_schema

REF='cr_'+'a'*32


def tools():
    return compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['revise_creation'])))


def schema(items):
    return next(t['function']['parameters'] for t in items if t['function']['name']=='word.revise_creation')


def arguments(count):
    return {'ref':REF,'changes':[{'op':'remove','path':f'blocks.{n}'} for n in range(count)]}


@pytest.mark.parametrize('pinned',[False,True])
def test_actual_native_grammar_enforces_one_to_four_changes(pinned):
    projected=tools()
    if pinned:projected=creation_state_tools(projected,{'status':'invalid_design','ref':REF})
    regex=native_tool_regex(projected)
    for count in (0,1,4,5,24):
        args=arguments(count)
        matched=re.fullmatch(regex,'<tool_call>'+json.dumps({'name':'word.revise_creation','arguments':args},separators=(',',':'))+'</tool_call>')
        assert bool(matched)==(1<=count<=4)
        if 1<=count<=4:validate_native_arguments('word.revise_creation',args,projected)
        else:
            with pytest.raises(HarnessError):validate_native_arguments('word.revise_creation',args,projected)


def test_host_24_and_full_value_path_semantics_remain_unchanged():
    assert len(ReviseCreation(**arguments(24)).changes)==24
    assert ReviseCreation.model_json_schema()['properties']['changes']['maxItems']==24
    values=[None,False,12.5,'Exact Δ',{'kind':'paragraph','text':'Full replacement'},['a',{'b':1}]]
    projected=tools()
    for value in values:
        args={'ref':REF,'changes':[{'op':'set','path':'blocks','value':value}]}
        validate_native_arguments('word.revise_creation',args,projected)
        assert ReviseCreation(**args).changes[0].value==value
    # Only the intentional native path syntax projection differs; values stay unrestricted.
    native=schema(projected)['$defs']['CreationChange']
    host=ReviseCreation.model_json_schema()['$defs']['CreationChange']
    # Compare after the existing enum/type compaction, independently of the
    # new array bound. This preserves the entire item schema, not just samples.
    host=compact_tools([{'type':'function','function':{
        'name':'word.example','parameters':host}}])[0]['function']['parameters']
    expected=copy.deepcopy(host)
    expected['properties']['path']['pattern']=creation_revision_path_pattern()
    assert native==expected
    assert native['properties']['value']==host['properties']['value']=={}


def test_projection_documents_budget_without_mutating_inputs_or_inserting_markers():
    full=project_schema(WORD_ACTION.json_schema(),['revise_creation']);before=copy.deepcopy(full)
    projected=compact_tools(action_tools(full))
    assert full==before
    function=next(t['function'] for t in projected if t['function']['name']=='word.revise_creation')
    assert '1–4 changes per native call' in function['description']
    assert 'new proposal ref' in function['description']
    assert 'no automatic splitting' in function['description']
    assert 'prepare_creation' in function['description']
    before=copy.deepcopy(projected)
    structural_schema(schema(projected))
    assert projected==before


@pytest.mark.parametrize('alteration',['larger','smaller','bool_bound','wrong_item_ref','extra_property',
                                     'different_fields','recursive_value','arbitrary_array','host_schema'])
def test_object_array_bounds_retained_only_for_exact_native_revision_shape(alteration):
    original=schema(tools());value=copy.deepcopy(original)
    if alteration=='larger':value['properties']['changes']['maxItems']=24
    elif alteration=='smaller':value['properties']['changes']['maxItems']=3
    elif alteration=='bool_bound':value['properties']['changes']['minItems']=True
    elif alteration=='wrong_item_ref':value['properties']['changes']['items']={'$ref':'#/$defs/Other'}
    elif alteration=='extra_property':value['properties']['other']={'type':'string'}
    elif alteration=='different_fields':value['$defs']['CreationChange']['properties']['extra']={'type':'string'}
    elif alteration=='recursive_value':value['$defs']['CreationChange']['properties']['value']={'$ref':'#/$defs/CreationChange'}
    elif alteration=='arbitrary_array':value={'type':'array','items':{'type':'object'},'minItems':1,'maxItems':4}
    else:value=ReviseCreation.model_json_schema()
    projected=structural_schema(value)
    array=projected if alteration=='arbitrary_array' else projected['properties']['changes']
    assert 'minItems' not in array and 'maxItems' not in array
    assert structural_schema(original)['properties']['changes']['maxItems']==4
