"""Exact evidence representation at the native tool boundary."""
import copy
import json
import re
import pytest
from localoffice.native_tools import requirement_evidence_tools,validate_native_arguments,action_tools,compact_tools
from localoffice.runtime import WORD_ACTION
from localoffice.word.guidance import project_schema
from localoffice.word.objective_evidence import catalogue_text
from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError


def tools_for(text):
    raw=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['plan_requirements'])))
    saved=copy.deepcopy(raw)
    projected=requirement_evidence_tools(raw,catalogue_text(text))
    assert raw==saved
    return [t for t in projected if t['function']['name'] in ('word.plan_requirements','ask_user')]


def args(evidence):
    return {'source_hash':'a'*64,'requirements':[{'kind':'page_geometry',**evidence,'field':'margin','equals':1.0,'unit':'in'}]}


@pytest.mark.parametrize('evidence,valid',[
    ({'source_range':{'start':'s01','end':'s02'}},True),
    ({'source_range':{'start':'s01','end':'s24'}},False),
    ({'source_quote':'First.'},False),
    ({'source_range':None},False),
    ({'source_range':{'start':'s01','end':'s01'},'source_quote':'First.'},False),
])
def test_native_exact_id_enum_and_single_representation(evidence,valid):
    tools=tools_for('First. Second.')
    value=args(evidence)
    raw='<tool_call>'+json.dumps({'name':'word.plan_requirements','arguments':value},separators=(',',':'))+'</tool_call>'
    assert bool(re.fullmatch(native_tool_regex(tools),raw))==valid
    if valid:validate_native_arguments('word.plan_requirements',value,tools)
    else:
        with pytest.raises(HarnessError):validate_native_arguments('word.plan_requirements',value,tools)


def test_unavailable_catalogue_preserves_quote_fallback():
    tools=tools_for('x'*3000)
    validate_native_arguments('word.plan_requirements',args({'source_quote':'x'}),tools)
    with pytest.raises(HarnessError):
        validate_native_arguments('word.plan_requirements',args({'source_range':{'start':'s01','end':'s01'}}),tools)
