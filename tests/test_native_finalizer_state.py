"""Fresh proposal facts constrain native syntax, never invent a binding."""
import copy
import json
import re
import pytest
from localoffice.native_tools import action_tools,compact_tools,creation_state_tools,validate_native_arguments
from localoffice.decoding import native_tool_regex
from localoffice.word.guidance import project_schema
from localoffice.runtime import WORD_ACTION
from localoffice.errors import HarnessError

REF='cr_'+'a'*32

def tools():return compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['finalize_creation'])))

def test_empty_missing_styles_and_identity_are_constrained_in_actual_grammar():
    original=tools();before=copy.deepcopy(original)
    exposed=creation_state_tools(original,{'status':'prepared','ref':REF,'missing_styles':[]})
    assert original==before
    regex=native_tool_regex(exposed)
    def matches(args):return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':'word.finalize_creation','arguments':args})+'</tool_call>')
    assert matches({'ref':REF,'styles':[]})
    assert matches({'ref':REF})
    assert not matches({'ref':'cr_'+'b'*32,'styles':[]})
    assert not matches({'ref':REF,'styles':[{'id':'AlreadyDefined'}]})
    validate_native_arguments('word.finalize_creation',{'ref':REF,'styles':[]},exposed)
    assert any(t['function']['name']=='ask_user' for t in exposed)

@pytest.mark.parametrize('state',[None,{'status':'stale','ref':REF,'missing_styles':[]}])
def test_no_fresh_state_does_not_imply_empty_styles(state):
    original=tools()
    assert creation_state_tools(original,state)==original


def test_missing_styles_remain_declarable_and_source_bindings_not_inferred():
    exposed=creation_state_tools(tools(),{'status':'prepared','ref':REF,'missing_styles':[{'id':'Band','slots':['footer.style']}]})
    args={'ref':REF,'styles':[{'id':'Band','font':'Arial','size_points':9}]}
    validate_native_arguments('word.finalize_creation',args,exposed)
    params=next(t['function']['parameters'] for t in exposed if t['function']['name']=='word.finalize_creation')
    assert 'const' not in params['properties']['styles']
    assert 'source_bindings' not in params['properties']
    assert 'bindings' not in args


def test_invalid_fresh_identity_fails_closed():
    with pytest.raises(HarnessError):creation_state_tools(tools(),{'status':'prepared','ref':'invented','missing_styles':[]})


@pytest.mark.parametrize('omitted',[False,True])
def test_only_complete_slot_projection_can_constrain_binding_paths(omitted):
    proposal={'status':'prepared','ref':REF,'missing_styles':[],
              'proposed_literals':{'items':[{'slot':'footer.text','text':'Page '}],'omitted':omitted}}
    exposed=creation_state_tools(tools(),proposal)
    params=next(t['function']['parameters'] for t in exposed if t['function']['name']=='word.finalize_creation')
    slot=params['$defs']['CreationSourceBinding']['properties']['slot']
    assert ('enum' in slot)==(not omitted)
    if not omitted:
        assert slot['enum']==['footer.text']
        with pytest.raises(HarnessError):validate_native_arguments('word.finalize_creation',
            {'ref':REF,'bindings':[{'slot':'header.text','ref':'ct_'+'a'*24}]},exposed)
    validate_native_arguments('word.finalize_creation',
        {'ref':REF,'bindings':[{'slot':'footer.text','ref':'ct_'+'a'*24}]},exposed)
