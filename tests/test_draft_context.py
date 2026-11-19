"""Scoped drafting preserves exact text and cannot emit file/finish tools."""
import copy
import json
import re
import pytest
from localoffice.word.draft_context import build
from localoffice.native_tools import validate_native_arguments,validate_history
from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError

REF='df_'+'a'*32

def test_exact_selected_instruction_decisions_and_legal_exit():
    instruction='  Escreva “olá”.\nDo not invent facts.  '
    decisions=[{'language':'Portuguese','scope':'draft'}];original=copy.deepcopy(decisions)
    messages,tools=build(instruction,REF,constraints=decisions)
    assert messages[1]=={'role':'user','content':instruction}
    assert 'Portuguese' in messages[0]['content'] and decisions==original
    validate_history(messages)
    assert {t['function']['name'] for t in tools}=={'word.submit_draft','ask_user'}
    validate_native_arguments('word.submit_draft',{'ref':REF,'text':'Olá.'},tools)
    validate_native_arguments('ask_user',{'text':'Which audience?'},tools)
    regex=native_tool_regex(tools)
    for name,arguments,allowed in [('word.submit_draft',{'ref':REF,'text':'Olá.'},True),
        ('word.submit_draft',{'ref':'df_'+'b'*32,'text':'Olá.'},False),
        ('finish',{'text':'Done'},False),('word.create',{'path':'report.docx'},False)]:
        raw='<tool_call>'+json.dumps({'name':name,'arguments':arguments},ensure_ascii=False,separators=(',',':'))+'</tool_call>'
        assert bool(re.fullmatch(regex,raw))==allowed

@pytest.mark.parametrize('text,ref',[('',REF),('   ',REF),('x'*65537,REF),('\ud800',REF),('Draft','cr_'+'a'*32)])
def test_invalid_focus_rejected_without_silent_truncation(text,ref):
    with pytest.raises(HarnessError):build(text,ref)

def test_oversized_decisions_rejected_without_scope_loss():
    with pytest.raises(HarnessError):build('Draft',REF,constraints=[{'note':'x'*8193}])


def test_planning_tool_uses_complete_catalogue_without_selecting_instruction():
    from localoffice.word.objective_evidence import catalogue_text
    from localoffice.word.draft_context import planning_tool
    from localoffice.native_tools import validate_native_arguments
    from localoffice.storage import digest
    source = 'Keep the literal. Draft an invitation. Keep the ending.'
    catalogue = catalogue_text(source)
    tool = planning_tool(catalogue)
    assert tool['function']['name'] == 'word.plan_draft'
    args = {'source_hash': digest(source.encode()), 'source_range': {'start': 's02', 'end': 's02'}}
    validate_native_arguments('word.plan_draft', args, [tool])
    for bad in ({**args, 'source_hash': '0' * 64},
                {**args, 'source_range': {'start': 's99', 'end': 's99'}},
                {**args, 'text': 'A canned response'}):
        with pytest.raises(HarnessError):
            validate_native_arguments('word.plan_draft', bad, [tool])
    assert tool['function']['parameters']['properties']['source_range']['properties']['start']['enum'] == ['s01', 's02', 's03']
    regex = native_tool_regex([tool, build('Draft', REF)[1][1]])
    for arguments, allowed in [(args, True), ({**args, 'source_hash': '0' * 64}, False),
                               ({**args, 'source_range': {'start': 's99', 'end': 's99'}}, False)]:
        wire = '<tool_call>' + json.dumps({'name': 'word.plan_draft', 'arguments': arguments}, separators=(',', ':')) + '</tool_call>'
        assert bool(re.fullmatch(regex, wire)) is allowed


def test_planning_tool_abstains_for_unavailable_catalogue_and_rejects_malformed_complete():
    from localoffice.word.draft_context import planning_tool
    assert planning_tool(None) is None
    assert planning_tool({'available': False, 'complete': False}) is None
    with pytest.raises(HarnessError):
        planning_tool({'available': True, 'complete': True, 'source_hash': 'a' * 64,
                       'segments': {'s02': 'out of order'}})
