"""Role-bound finalization from complete facts, including actual Outlines regex."""
import copy
import json
import re

import pytest

from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError
from localoffice.native_tools import action_tools,compact_tools,creation_state_tools,validate_native_arguments
from localoffice.runtime import WORD_ACTION
from localoffice.word.contracts import FinalizeCreation
from localoffice.word.guidance import project_schema

REF='cr_'+'a'*32
REFS={role:['ct_'+format(n,'024x'),'ct_'+format(n+10,'024x')]
      for n,role in enumerate(('paragraph','header','footer','table_cell'),1)}
SLOTS={'blocks.0.text':'paragraph','blocks.1.rows.0.0':'table_cell',
       **{name+'.text':name.rsplit('_',1)[-1] for name in
          ('header','footer','first_header','first_footer','even_header','even_footer')}}


def inputs(text="Identical text across roles"):
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['finalize_creation','repair_creation'])))
    proposal={'status':'prepared','ref':REF,'missing_styles':[],
              'proposed_literals':{'omitted':False,'items':[{'slot':slot,'text':text} for slot in SLOTS]}}
    active={'count':8,'truncated':False,'by_role':{
        role:{ref:'Identical text across roles' for ref in refs} for role,refs in REFS.items()}}
    return tools,proposal,active


def arguments(slot=None,ref=None):
    value={'ref':REF,'styles':[]}
    if slot is not None:value['bindings']=[{'slot':slot,'ref':ref}]
    return value


@pytest.fixture(scope='module',params=['Identical text across roles',''])
def projected(request):
    tools,proposal,active=inputs(request.param)
    result=creation_state_tools(tools,proposal,active_plan=active)
    return result,native_tool_regex(result)


def matches(regex,args,name='word.finalize_creation'):
    return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':name,'arguments':args},separators=(',',':'))+'</tool_call>')


def test_all_same_role_identity_or_empty_choices_across_variants_and_cells_are_allowed(projected):
    tools,regex=projected
    for slot,role in SLOTS.items():
        for ref in REFS[role]:
            args=arguments(slot,ref)
            validate_native_arguments('word.finalize_creation',args,tools)
            assert matches(regex,args)
    for args in (arguments(),{**arguments(),'bindings':[]}):
        validate_native_arguments('word.finalize_creation',args,tools)
        assert matches(regex,args)
    branch=next(t['function'] for t in tools if t['function']['name']=='word.finalize_creation')
    assert len(branch['parameters']['$defs']['CreationSourceBinding']['oneOf'])==4


def test_cross_role_refs_unknown_slots_and_refs_rejected_in_schema_and_grammar(projected):
    tools,regex=projected
    invalid=[arguments(slot,REFS[other][0]) for slot,role in SLOTS.items() for other in REFS if other!=role]
    invalid += [arguments('blocks.2.text',REFS['paragraph'][0]),arguments('header.text','ct_'+'f'*24)]
    for args in invalid:
        with pytest.raises(HarnessError):validate_native_arguments('word.finalize_creation',args,tools)
        assert not matches(regex,args)


@pytest.mark.parametrize('case',['absent','truncated','stale','preview','missing_count','wrong_count','unknown_role','bad_ref','duplicate_ref'])
def test_incomplete_active_evidence_preserves_original_binding_capability(case):
    tools,proposal,active=inputs()
    baseline=creation_state_tools(tools,proposal)
    if case=='absent':active=None
    elif case=='truncated':active['truncated']=True
    elif case=='stale':active['status']='plan_repair_required'
    elif case=='preview':active['by_role']['header'][REFS['header'][0]]={'preview':'x','truncated':True}
    elif case=='missing_count':del active['count']
    elif case=='wrong_count':active['count']=7
    elif case=='unknown_role':active['by_role']['unknown']={}
    elif case=='bad_ref':active['by_role']['header']['invalid']='Text'
    elif case=='duplicate_ref':active['by_role']['header'][REFS['footer'][0]]='Text'
    assert creation_state_tools(tools,proposal,active_plan=active)==baseline


@pytest.mark.parametrize('slot',['blocks.01.text','blocks.-1.text','blocks.1.rows.0.01','header.other','unknown.text'])
def test_unrecognized_slot_does_not_narrow_any_binding(slot):
    tools,proposal,active=inputs()
    proposal['proposed_literals']['items'].append({'slot':slot})
    projected=creation_state_tools(tools,proposal,active_plan=active)
    for tool in projected:
        if tool['function']['name'] not in ('word.finalize_creation','word.repair_creation'):continue
        binding=tool['function']['parameters']['$defs']['CreationSourceBinding']
        assert 'oneOf' not in binding and 'enum' not in binding['properties']['slot']


@pytest.mark.parametrize('case',['omitted','duplicate','absent','stale_proposal'])
def test_incomplete_proposal_falls_back(case):
    tools,proposal,active=inputs()
    if case=='omitted':proposal['proposed_literals']['omitted']=True
    elif case=='duplicate':proposal['proposed_literals']['items'].append(proposal['proposed_literals']['items'][0])
    elif case=='absent':proposal.pop('proposed_literals')
    else:proposal['status']='stale'
    assert creation_state_tools(tools,proposal,active_plan=active)==creation_state_tools(tools,proposal)


def test_no_refs_for_proven_role_cannot_bind_but_empty_bindings_stay_legal():
    tools,proposal,active=inputs()
    del active['by_role']['footer'];active['count']=6
    projected=creation_state_tools(tools,proposal,active_plan=active)
    regex=native_tool_regex(projected)
    assert matches(regex,{**arguments(),'bindings':[]})
    assert not matches(regex,arguments('footer.text',REFS['header'][0]))
    assert matches(regex,arguments('header.text',REFS['header'][0]))
    empty=creation_state_tools(tools,proposal,active_plan={'count':0,'truncated':False,'by_role':{}})
    regex=native_tool_regex(empty)
    assert matches(regex,arguments()) and matches(regex,{**arguments(),'bindings':[]})
    assert not matches(regex,arguments('header.text',REFS['header'][0]))


def test_inputs_host_contract_and_missing_styles_remain_unchanged():
    tools,proposal,active=inputs();proposal['status']='missing_styles';proposal['missing_styles']=['Band']
    before=copy.deepcopy((tools,proposal,active))
    result=creation_state_tools(tools,proposal,active_plan=active)
    assert (tools,proposal,active)==before
    finalize=next(t['function'] for t in result if t['function']['name']=='word.finalize_creation')
    assert 'const' not in finalize['parameters']['properties']['styles']
    # Native role projection does not remove legacy host representations or
    # replace authoritative role/slot validation in the execution resolver.
    request=FinalizeCreation(ref=REF,source_bindings={'footer.text':REFS['footer'][0]})
    assert request.source_bindings=={'footer.text':REFS['footer'][0]}
    with pytest.raises(ValueError):FinalizeCreation(ref=REF,bindings=[
        {'slot':'footer.text','ref':REFS['footer'][0]}]*2)


def test_repair_tool_uses_same_role_grammar_and_accepts_explicit_missing_styles():
    tools,proposal,active=inputs();proposal['missing_styles']=['Band'];proposal['status']='missing_styles'
    tools=creation_state_tools(tools,proposal,active_plan=active)
    args={'ref':REF,'styles':[{'id':'Band','font':'Arial','size_points':9}],
          'bindings':[{'slot':'footer.text','ref':REFS['footer'][0]}]}
    validate_native_arguments('word.repair_creation',args,tools)
    regex=native_tool_regex(tools)
    assert matches(regex,args,name='word.repair_creation')
    args['bindings'][0]['ref']=REFS['header'][0]
    assert not matches(regex,args,name='word.repair_creation')


def test_invalid_design_pins_only_revision_and_never_exposes_finalization():
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),
        ['revise_creation','finalize_creation','repair_creation'])))
    _,proposal,active=inputs();proposal['status']='invalid_design'
    before=copy.deepcopy((tools,proposal,active))
    projected=creation_state_tools(tools,proposal,active_plan=active)
    names={tool['function']['name'] for tool in projected}
    assert 'word.revise_creation' in names
    assert 'word.finalize_creation' not in names and 'word.repair_creation' not in names
    revise=next(tool['function'] for tool in projected if tool['function']['name']=='word.revise_creation')
    assert revise['parameters']['properties']['ref']=={'const':REF}
    args={'ref':REF,'changes':[{'op':'remove','path':'blocks.1'}]}
    validate_native_arguments('word.revise_creation',args,projected)
    with pytest.raises(HarnessError):
        validate_native_arguments('word.revise_creation',{**args,'ref':'cr_'+'b'*32},projected)
    assert (tools,proposal,active)==before


def test_stale_invalid_proposal_does_not_pin_a_revision_handle():
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['revise_creation'])))
    _,proposal,active=inputs();proposal['status']='stale'
    assert creation_state_tools(tools,proposal,active_plan=active)==tools


@pytest.mark.parametrize('text',['Unrelated proposed text','Identical text across roles '])
def test_competing_plain_text_cannot_be_replaced_even_by_same_role(text):
    tools,proposal,active=inputs(text)
    projected=creation_state_tools(tools,proposal,active_plan=active)
    regex=native_tool_regex(projected)
    for tool_name in ('word.finalize_creation','word.repair_creation'):
        for slot,role in SLOTS.items():
            args=arguments(slot,REFS[role][0])
            if tool_name=='word.repair_creation':args['styles']=[{'id':'Band'}]
            with pytest.raises(HarnessError):validate_native_arguments(tool_name,args,projected)
            assert not matches(regex,args,name=tool_name)
    assert matches(regex,arguments()) and matches(regex,{**arguments(),'bindings':[]})


def test_partial_text_does_not_claim_exact_identity_and_keeps_host_fallback():
    tools,proposal,active=inputs('Unrelated proposed text')
    proposal['proposed_literals']['items'][0]={'slot':'blocks.0.text','preview':'Unrelated','truncated':True}
    projected=creation_state_tools(tools,proposal,active_plan=active)
    regex=native_tool_regex(projected)
    # Complete text is unavailable: native role scope survives, but exact
    # content identity remains mandatory in the authoritative host resolver.
    assert matches(regex,arguments('blocks.0.text',REFS['paragraph'][0]))
    assert not matches(regex,arguments('blocks.0.text',REFS['footer'][0]))
