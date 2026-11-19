"""Identity-only binding projection never replaces drafted or competing text."""
import copy
import json
import re
import pytest
from localoffice.decoding import native_tool_regex
from localoffice.native_tools import action_tools,compact_tools,creation_state_tools,validate_native_arguments
from localoffice.errors import HarnessError
from localoffice.word.contracts import FinalizeCreation

REF='cr_'+'a'*32
A='ct_'+'1'*24
B='ct_'+'2'*24
C='ct_'+'3'*24


def inputs():
    tools=compact_tools(action_tools(FinalizeCreation.model_json_schema()))
    tools.append({'type':'function','function':{'name':'ask_user','parameters':{
        'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}})
    proposal={'status':'prepared','ref':REF,'missing_styles':[], 'proposed_literals':{
        'omitted':False,'items':[{'slot':'blocks.0.text','text':'Exact'},
            {'slot':'blocks.1.text','text':'Please review the document and share your helpful comments.'},
            {'slot':'blocks.2.text','text':'Conclusion'}, {'slot':'blocks.3.text','text':''}]}}
    active={'count':3,'truncated':False,'by_role':{'paragraph':{A:'Exact',B:'Conclusion'},'footer':{C:'Exact'}}}
    return tools,proposal,active


def match(regex,slot,ref):
    args={'ref':REF,'styles':[],'bindings':[{'slot':slot,'ref':ref}]}
    return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':'word.finalize_creation','arguments':args},separators=(',',':'))+'</tool_call>') is not None


def test_exact_identity_and_empty_slots_only_actual_grammar():
    tools,proposal,active=inputs();before=copy.deepcopy((tools,proposal,active))
    projected=creation_state_tools(tools,proposal,active)
    regex=native_tool_regex(projected)
    assert match(regex,'blocks.0.text',A)
    assert match(regex,'blocks.2.text',B)
    for ref in (A,B):assert match(regex,'blocks.3.text',ref)
    for ref in (A,B,C):assert not match(regex,'blocks.1.text',ref)
    assert not match(regex,'blocks.0.text',B)
    assert not match(regex,'blocks.0.text',C)
    args={'ref':REF,'styles':[],'bindings':[{'slot':'blocks.1.text','ref':B}]}
    with pytest.raises(HarnessError):validate_native_arguments('word.finalize_creation',args,projected)
    assert (tools,proposal,active)==before


@pytest.mark.parametrize('text',['exact','Exact ','Ｅxact','Exac'])
def test_no_case_whitespace_unicode_or_prefix_matching(text):
    tools,proposal,active=inputs();proposal['proposed_literals']['items'][0]['text']=text
    regex=native_tool_regex(creation_state_tools(tools,proposal,active))
    assert not match(regex,'blocks.0.text',A)


@pytest.mark.parametrize('case',['truncated','preview','missing_text','omitted','active_preview','active_stale'])
def test_partial_evidence_leaves_host_validation_fallback(case):
    tools,proposal,active=inputs()
    if case=='truncated':proposal['proposed_literals']['items'][0]['truncated']=True
    elif case=='preview':proposal['proposed_literals']['items'][0]={'slot':'blocks.0.text','preview':'Ex','truncated':True}
    elif case=='missing_text':proposal['proposed_literals']['items'][0].pop('text')
    elif case=='omitted':proposal['proposed_literals']['omitted']=True
    elif case=='active_preview':active['by_role']['paragraph'][A]={'preview':'Ex','truncated':True}
    else:active['status']='plan_repair_required'
    regex=native_tool_regex(creation_state_tools(tools,proposal,active))
    assert match(regex,'blocks.0.text',B)  # Host still enforces exact identity.


def test_existing_bound_slot_resolves_only_from_complete_same_role_evidence():
    tools,proposal,active=inputs();proposal['proposed_literals']['items'][0]={'slot':'blocks.0.text','ref':A}
    regex=native_tool_regex(creation_state_tools(tools,proposal,active))
    assert match(regex,'blocks.0.text',A) and not match(regex,'blocks.0.text',B)


def test_all_competing_drafts_allow_only_empty_bindings():
    tools,proposal,active=inputs()
    proposal['proposed_literals']['items']=[{'slot':'blocks.0.text','text':'Drafted text'}]
    result=creation_state_tools(tools,proposal,active)
    assert result[0]['function']['parameters']['properties']['bindings']=={'const':[]}
    validate_native_arguments('word.finalize_creation',{'ref':REF,'styles':[],'bindings':[]},result)


@pytest.mark.parametrize('slot,role',[('blocks.0.text','paragraph'),('blocks.1.rows.0.0','table_cell'),
    ('header.text','header'),('footer.text','footer'),('first_header.text','header'),
    ('first_footer.text','footer'),('even_header.text','header'),('even_footer.text','footer')])
@pytest.mark.parametrize('text',['Exact',''])
def test_identity_and_empty_binding_across_all_existing_slot_types(slot,role,text):
    tools,proposal,active=inputs()
    proposal['proposed_literals']['items']=[{'slot':slot,'text':text}]
    active={'count':2,'truncated':False,'by_role':{role:{A:'Exact',B:'Different'}}}
    regex=native_tool_regex(creation_state_tools(tools,proposal,active))
    assert match(regex,slot,A)
    assert match(regex,slot,B)==(text=='')


@pytest.mark.parametrize('legacy',[False,True])
def test_existing_binding_keeps_same_reference_even_when_other_text_is_identical(tmp_path,legacy):
    from localoffice.word.creation_repair import _proposed_literals
    payload={'blocks':[{'kind':'paragraph','text':'' if legacy else {'ref':A}}]}
    if legacy:payload['literal_refs']={'blocks.0.text':A}
    tools,proposal,active=inputs()
    proposal['proposed_literals']=_proposed_literals(payload)
    assert proposal['proposed_literals']['items'][0]['ref']==A
    active={'count':2,'truncated':False,'by_role':{'paragraph':{A:'Identical',B:'Identical'}}}
    regex=native_tool_regex(creation_state_tools(tools,proposal,active))
    assert match(regex,'blocks.0.text',A)
    assert not match(regex,'blocks.0.text',B)


@pytest.mark.parametrize('text',['Identical',''])
def test_plain_or_empty_slot_keeps_all_same_role_equal_text_references(text):
    tools,proposal,active=inputs()
    proposal['proposed_literals']['items']=[{'slot':'blocks.0.text','text':text}]
    active={'count':2,'truncated':False,'by_role':{'paragraph':{A:'Identical',B:'Identical'}}}
    regex=native_tool_regex(creation_state_tools(tools,proposal,active))
    assert match(regex,'blocks.0.text',A) and match(regex,'blocks.0.text',B)
