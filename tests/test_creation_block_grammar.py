"""Actual native regex enforces only the trusted bounded typed block envelope."""
import copy
import json
import re
import pytest
from pydantic import ValidationError
from localoffice.decoding import native_tool_regex,structural_schema
from localoffice.native_tools import action_tools,compact_tools,creation_state_tools
from localoffice.word.contracts import EditCreationBlocks,ReviseCreation

REF='cr_'+'a'*32


def tools():
    result=action_tools(EditCreationBlocks.model_json_schema())+action_tools(ReviseCreation.model_json_schema())
    result.append({'type':'function','function':{'name':'ask_user','parameters':{
        'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}}})
    return compact_tools(result)


@pytest.fixture(scope='module')
def regex():
    return native_tool_regex(creation_state_tools(tools(),{'ref':REF,'status':'invalid_design'}))


def matches(regex,changes,ref=REF,name='word.edit_creation_blocks'):
    return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':name,'arguments':{
        'ref':ref,'changes':changes}},separators=(',',':'))+'</tool_call>') is not None


def test_actual_grammar_bounds_both_operations_and_pin(regex):
    remove={'op':'remove_block','index':3}
    patch={'op':'set_paragraph','index':2,'page_break_before':True}
    assert matches(regex,[remove,patch])
    assert matches(regex,[remove]*4)
    assert not matches(regex,[])
    assert not matches(regex,[remove]*5)
    assert not matches(regex,[remove],ref='cr_'+'b'*32)
    assert matches(regex,[{'op':'remove','path':'blocks.3'}],name='word.revise_creation')


@pytest.mark.parametrize('change',[
    False,{'op':'set_paragraph','index':1,'text':False},
    {'op':'set_paragraph','index':1,'style':False},
    {'op':'set_paragraph','index':1,'page_break_before':'false'},
    {'op':'set_paragraph','index':1,'path':'blocks.1','value':False},
    {'op':'remove_block','index':1,'value':False},
])
def test_grammar_rejects_boolean_blocks_and_generic_paths(regex,change):
    assert not matches(regex,[change])


def test_sparse_requirement_remains_host_validated_but_omission_is_legal(regex):
    empty={'op':'set_paragraph','index':1}
    assert matches(regex,[empty])
    with pytest.raises(ValidationError):EditCreationBlocks(ref=REF,changes=[empty])
    for patch in [{'op':'set_paragraph','index':1,'page_break_before':False},
                  {'op':'set_paragraph','index':1,'text':'Draft'},
                  {'op':'set_paragraph','index':1,'style':'Body'}]:
        assert matches(regex,[patch])
        EditCreationBlocks(ref=REF,changes=[patch])


@pytest.mark.parametrize('field',['text','style','page_break_before'])
@pytest.mark.parametrize('mixed',[False,True])
def test_explicit_null_rejected_even_with_other_valid_property(regex,field,mixed):
    patch={'op':'set_paragraph','index':1}
    if mixed:patch['text' if field!='text' else 'style']='Body'
    patch[field]=None
    # Serialize optional properties in the schema order used by native regex.
    patch={key:patch[key] for key in ('op','index','text','style','page_break_before') if key in patch}
    assert not matches(regex,[patch])
    with pytest.raises(ValidationError):EditCreationBlocks(ref=REF,changes=[patch])


def test_projection_does_not_change_host_contract_or_other_native_tools():
    original=EditCreationBlocks.model_json_schema();before=copy.deepcopy(original)
    result=compact_tools(action_tools(original))[0]['function']['parameters']
    assert original==before==EditCreationBlocks.model_json_schema()
    host_fields=original['$defs']['SetCreationParagraph']['properties']
    fields=result['$defs']['SetCreationParagraph']['properties']
    for name in ('text','style','page_break_before'):
        assert {'type':'null'} in host_fields[name]['anyOf']
        assert {'type':'null'} not in fields[name].get('anyOf',[])
    assert result['$defs']['SetCreationParagraph']['required']==['op','index']
    original['properties']['tool']['const']='word.other'
    other=action_tools(original)[0]['function']['parameters']
    assert {'type':'null'} in other['$defs']['SetCreationParagraph']['properties']['text']['anyOf']


@pytest.mark.parametrize('mutation',['bounds','extra','definition','op','null'])
def test_nearby_untrusted_shapes_do_not_keep_array_bounds(mutation):
    parameters=tools()[0]['function']['parameters'];original=copy.deepcopy(parameters)
    projected=structural_schema(parameters)
    assert projected['properties']['changes']['minItems']==1
    assert projected['properties']['changes']['maxItems']==4
    assert parameters==original
    if mutation=='bounds':parameters['properties']['changes']['maxItems']=5
    elif mutation=='extra':parameters['properties']['path']={'type':'string'}
    elif mutation=='definition':parameters['$defs']['RemoveCreationBlock']['properties']['index']={'type':'boolean'}
    elif mutation=='op':parameters['$defs']['RemoveCreationBlock']['properties']['op']={'const':'invented'}
    else:parameters['$defs']['SetCreationParagraph']['properties']['text']['anyOf'].append({'type':'null'})
    result=structural_schema(parameters)['properties']['changes']
    assert 'maxItems' not in result and 'minItems' not in result


def test_invalid_projection_keeps_generic_fallback_and_stale_does_not_pin():
    original=tools();snapshot=copy.deepcopy(original)
    projected=creation_state_tools(original,{'ref':REF,'status':'invalid_design'})
    assert original==snapshot
    for name in ('word.edit_creation_blocks','word.revise_creation'):
        tool=next(t for t in projected if t['function']['name']==name)
        assert tool['function']['parameters']['properties']['ref']=={'const':REF}
    assert creation_state_tools(original,{'ref':REF,'status':'stale'})==original


def focused_state():
    return {'status':'invalid_design','ref':REF,
            'validation_errors':{'items':[{'code':'page_break_position','path':'blocks.3',
                'kind':'page_break','truncated':False,'reasons':['trailing']}],'omitted_count':0},
            'proposed_literals':{'items':[{'slot':'blocks.0.text'},{'slot':'blocks.1.text'},
                {'slot':'blocks.2.text'},{'slot':'blocks.4.rows.0.0'},{'slot':'footer.text'}],'omitted':False}}


def test_focused_indices_enforced_by_actual_regex_and_bounds_preserved():
    projected=creation_state_tools(tools(),focused_state())
    parameters=projected[0]['function']['parameters']
    assert parameters['$defs']['RemoveCreationBlock']['properties']['index']['enum']==[3]
    assert parameters['$defs']['SetCreationParagraph']['properties']['index']['enum']==[0,1,2]
    assert structural_schema(parameters)['properties']['changes']['maxItems']==4
    regex=native_tool_regex(projected)
    assert matches(regex,[{'op':'remove_block','index':3},{'op':'set_paragraph','index':2,'page_break_before':True}])
    assert not matches(regex,[{'op':'remove_block','index':1}])
    assert not matches(regex,[{'op':'set_paragraph','index':3,'page_break_before':True}])
    assert not matches(regex,[{'op':'set_paragraph','index':4,'text':'cell'}])
    assert not matches(regex,[{'op':'remove_block','index':3}]*5)
    assert matches(regex,[{'op':'remove','path':'blocks.1'}],name='word.revise_creation')
    assert 'Focused repair view' in projected[0]['function']['description']


@pytest.mark.parametrize('change',['omitted_errors','truncated_error','bad_path','duplicate_error','omitted_literals','bad_slot','duplicate_slot','stale'])
def test_incomplete_or_invalid_evidence_never_constrains_partial_indices(change):
    state=focused_state()
    if change=='omitted_errors':state['validation_errors']['omitted_count']=1
    elif change=='truncated_error':state['validation_errors']['items'][0]['truncated']=True
    elif change=='bad_path':state['validation_errors']['items'][0]['path']='blocks.24'
    elif change=='duplicate_error':state['validation_errors']['items']*=2
    elif change=='omitted_literals':state['proposed_literals']['omitted']=True
    elif change=='bad_slot':state['proposed_literals']['items'][0]['slot']='blocks.24.text'
    elif change=='duplicate_slot':state['proposed_literals']['items']*=2
    else:state['status']='stale'
    definitions=creation_state_tools(tools(),state)[0]['function']['parameters']['$defs']
    target='SetCreationParagraph' if change in ('omitted_literals','bad_slot','duplicate_slot') else 'RemoveCreationBlock'
    assert 'enum' not in definitions[target]['properties']['index']


@pytest.mark.parametrize('indices',[[],[True],[24],[-1],[1,1],[2,1],['1']])
def test_untrusted_index_enums_do_not_enable_bounded_array_exception(indices):
    parameters=tools()[0]['function']['parameters']
    parameters['$defs']['RemoveCreationBlock']['properties']['index']={'type':'integer','enum':indices}
    assert 'maxItems' not in structural_schema(parameters)['properties']['changes']


def layout_state():
    state=focused_state()
    state['block_layout']={'block_count':4,'paragraphs':[0,1,2],
                           'page_break_before':[2],'standalone_breaks':[3]}
    state['proposed_literals']={'items':[],'omitted':True}
    return state


def test_complete_layout_overrides_truncated_literal_navigation():
    state=layout_state()
    definitions=creation_state_tools(tools(),state)[0]['function']['parameters']['$defs']
    assert definitions['SetCreationParagraph']['properties']['index']['enum']==[0,1,2]
    assert definitions['RemoveCreationBlock']['properties']['index']['enum']==[3]
    state['validation_errors']['items'][0]['path']='blocks.1'
    definitions=creation_state_tools(tools(),state)[0]['function']['parameters']['$defs']
    assert 'enum' not in definitions['RemoveCreationBlock']['properties']['index']
    assert definitions['SetCreationParagraph']['properties']['index']['enum']==[0,1,2]


@pytest.mark.parametrize('change',[
    {'block_count':True},{'block_count':25},{'block_count':0},
    {'paragraphs':[0,0,2]},{'paragraphs':[False,1,2]},{'paragraphs':[0,4]},
    {'standalone_breaks':[2,3]},{'standalone_breaks':[3,3]},
    {'page_break_before':[3]},{'page_break_before':[2,2]},
    {'paragraphs':'0,1,2'},{'extra':False},
])
def test_invalid_layout_falls_back_without_narrowing(change):
    state=layout_state();state['block_layout'].update(change)
    definitions=creation_state_tools(tools(),state)[0]['function']['parameters']['$defs']
    for name in ('RemoveCreationBlock','SetCreationParagraph'):
        assert 'enum' not in definitions[name]['properties']['index']


def test_missing_layout_retains_previous_complete_evidence_behavior():
    definitions=creation_state_tools(tools(),focused_state())[0]['function']['parameters']['$defs']
    assert definitions['RemoveCreationBlock']['properties']['index']['enum']==[3]
    assert definitions['SetCreationParagraph']['properties']['index']['enum']==[0,1,2]


def test_zero_paragraph_layout_uses_explicit_unrestricted_set_fallback_not_empty_enum():
    state={'status':'invalid_design','ref':REF,
        'block_layout':{'block_count':2,'paragraphs':[],
                        'page_break_before':[],'standalone_breaks':[0,1]},
        'validation_errors':{'omitted_count':0,'items':[
            {'code':'page_break_position','path':f'blocks.{i}','kind':'page_break',
             'truncated':False,'reasons':['adjacent']} for i in (0,1)]},
        'proposed_literals':{'items':[],'omitted':False}}
    projected=creation_state_tools(tools(),state)
    parameters=projected[0]['function']['parameters']
    assert parameters['$defs']['RemoveCreationBlock']['properties']['index']['enum']==[0,1]
    # No paragraph target exists: retain the original host-validated index
    # schema. This is fallback, not permission to treat a break as a paragraph.
    assert 'enum' not in parameters['$defs']['SetCreationParagraph']['properties']['index']
    bounded=structural_schema(parameters)['properties']['changes']
    assert bounded['minItems']==1 and bounded['maxItems']==4
    regex=native_tool_regex(projected)
    assert matches(regex,[{'op':'remove_block','index':0}])
    assert not matches(regex,[])
    assert not matches(regex,[{'op':'remove_block','index':0}]*5)
    assert '"enum": []' not in json.dumps(projected)
