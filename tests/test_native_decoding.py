import json,re
import pytest
from localoffice.decoding import native_tool_regex,structural_schema
from localoffice.native_tools import action_tools,compact_tools
from localoffice.runtime import WORD_ACTION
from localoffice.word.guidance import project_schema


def test_native_grammar_has_exits_and_rejects_wrong_wrapper_names_and_arguments():
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['create_character_style'])))
    regex=native_tool_regex(tools)
    def matches(name,args):
        return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':name,'arguments':args},separators=(',',':'))+'</tool_call>')
    assert matches('finish',{'text':'Ready'})
    assert matches('ask_user',{'text':'Which style?'})
    assert not matches('invented',{})
    assert not re.fullmatch(regex,'The working copy is ready.')
    good={'path':'report.docx','revision':'a'*64,'operations':[{'target':'style:Body','expected_hash':'b'*64,'op':'create_character_style','style_id':'Callout','name':'Report callout','inherit_base':False,'properties':{'bold':True}}]}
    # JSON schema regex follows property order; serialize in declared order.
    schema=structural_schema(project_schema(WORD_ACTION.json_schema(),['create_character_style']))['$defs']['CreateCharacterStyle']
    operation=good['operations'][0]
    good['operations'][0]={key:operation[key] for key in schema['properties'] if key in operation}
    assert matches('word.stage_batch',good)
    del good['operations'][0]['expected_hash']
    assert not matches('word.stage_batch',good)


def test_native_grammar_requires_explicit_pause_exit():
    with pytest.raises(ValueError,match='ask_user pause exit'):native_tool_regex([])


def test_structural_projection_preserves_real_property_names():
    value={'type':'object','properties':{'title':{'type':'string','maxLength':3},'default':{'type':'boolean','default':False}},'required':['title']}
    result=structural_schema(value)
    assert set(result['properties'])=={'title','default'}
    assert result['properties']['title']=={'type':'string'}


def test_native_capability_loading_bounds_are_enforced_in_grammar():
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),[])))
    regex=native_tool_regex(tools)
    def matches(operations):
        call={'name':'word.load_tools','arguments':{'operations':operations}}
        return re.fullmatch(regex,'<tool_call>'+json.dumps(call,separators=(',',':'))+'</tool_call>')
    assert matches(['replace_text'])
    assert matches(['replace_text','format_text','set_style','format_paragraph'])
    assert not matches([])
    assert not matches(['replace_text','format_text','set_style','format_paragraph','set_list'])
    assert not matches(['invented'])


@pytest.mark.parametrize('schema',[
    {'type':'array','items':{'type':'object'},'minItems':1,'maxItems':4},
    {'type':'array','items':{'enum':['a','b']},'minItems':1,'maxItems':24},
    {'type':'array','items':{'enum':[1,2]},'minItems':1,'maxItems':4},
    {'type':'array','items':{'enum':['x'*81]},'minItems':1,'maxItems':4},
])
def test_other_array_limits_remain_host_validated(schema):
    result=structural_schema(schema)
    assert 'minItems' not in result and 'maxItems' not in result
    assert schema['maxItems']>=4


def test_small_enum_bounds_preserve_zero_and_do_not_mutate_schema():
    schema={'type':'array','items':{'enum':['a','b']},'minItems':0,'maxItems':2}
    before=json.dumps(schema)
    result=structural_schema(schema)
    assert result['minItems']==0 and result['maxItems']==2
    assert json.dumps(schema)==before


@pytest.mark.parametrize('identifier',['Normal','Header_1','A-b','A'+'x'*63,'A'+'x'*64])
def test_trusted_identifier_syntax_decodes_without_finite_length_automaton(identifier):
    from outlines_core.json_schema import build_regex_from_schema
    from localoffice.decoding import STYLE_ID_PATTERN,STYLE_ID_SYNTAX
    value={'type':'string','pattern':STYLE_ID_PATTERN}
    projected=structural_schema(value)
    assert projected['pattern']==STYLE_ID_SYNTAX and value['pattern']==STYLE_ID_PATTERN
    regex=build_regex_from_schema(json.dumps(projected))
    assert re.fullmatch(regex,json.dumps(identifier))
    assert '{0,63}' not in regex


@pytest.mark.parametrize('identifier',['Header 1','1Header','','Éditorial','Header\n'])
def test_trusted_identifier_syntax_refuses_invalid_names(identifier):
    from outlines_core.json_schema import build_regex_from_schema
    from localoffice.decoding import STYLE_ID_PATTERN
    regex=build_regex_from_schema(json.dumps(structural_schema({'type':'string','pattern':STYLE_ID_PATTERN})))
    assert not re.fullmatch(regex,json.dumps(identifier))


@pytest.mark.parametrize('value',[
    {'type':'string','pattern':'^[A-Za-z][A-Za-z0-9_-]{0,64}$'},
    {'type':'string','pattern':'^(a+)+$'},
    {'type':'integer','pattern':'^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
])
def test_identifier_projection_does_not_promote_other_patterns(value):
    assert 'pattern' not in structural_schema(value)


def test_native_creation_identifier_syntax_preserves_free_document_text():
    from localoffice.word.contracts import CreateDocument
    from pydantic import ValidationError
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['create'])))
    regex=native_tool_regex(tools)
    def matches(identifier):
        args={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Free design: café 😀'}],
              'header':{'text':'A header with spaces','style':identifier}}
        return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':'word.create','arguments':args},separators=(',',':'),ensure_ascii=False)+'</tool_call>')
    assert matches('Header_1')
    assert not matches('Header 1')
    assert matches('A'*65)  # Length is deliberately still a host check.
    with pytest.raises(ValidationError):
        CreateDocument(path='a.docx',blocks=[{'kind':'paragraph','text':'Body'}],styles=[{'id':'A'*65}])


def test_nonempty_source_quotes_are_enforced_by_native_grammar():
    from localoffice.native_tools import action_tools,compact_tools
    from localoffice.word.guidance import project_schema
    from localoffice.runtime import WORD_ACTION
    tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['plan_content'])))
    regex=native_tool_regex(tools)
    def match(quote):
        args={'literals':[{'role':'paragraph','quote':quote}]}
        return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':'word.plan_content','arguments':args},ensure_ascii=False)+'</tool_call>')
    assert not match('')
    for text in ['A','é','漢','😀','"','\\','\n',' ']:assert match(text),repr(text)
    # Nonempty syntax does not pretend to validate provenance or word counts.
    assert match('Not present in the source')
    assert structural_schema({'type':'string','minLength':1,'maxLength':8000})=={'type':'string','minLength':1}
    assert structural_schema({'type':'string','minLength':8,'maxLength':8000})=={'type':'string'}
