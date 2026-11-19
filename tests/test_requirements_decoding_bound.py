"""Trusted requirement-count grammar only; offline regex checks, no model."""
import copy
import json
import re
import time

import pytest

from localoffice import decoding
from localoffice.native_tools import action_tools, compact_tools, requirement_evidence_tools
from localoffice.runtime import WORD_ACTION
from localoffice.word.guidance import project_schema
from localoffice.word.objective_evidence import catalogue_text


def tools_for(mode, *, fixed_hash=False, compact=True, segments=2):
    tools=action_tools(project_schema(WORD_ACTION.json_schema(), ['plan_requirements']))
    if compact:
        tools=compact_tools(tools)
    text=' '.join(f'Sentence {index}.' for index in range(segments)) if mode=='range' else 'x'*3000
    if mode!='unprojected':
        tools=requirement_evidence_tools(tools, catalogue_text(text))
    tools=[tool for tool in tools if tool['function']['name'] in ('word.plan_requirements','ask_user')]
    if fixed_hash:
        parameters(tools)['properties']['source_hash']={'const':'a'*64}
    return tools


def parameters(tools):
    return next(tool['function']['parameters'] for tool in tools
                if tool['function']['name']=='word.plan_requirements')


def payload(count, mode):
    evidence={'source_range':{'start':'s01','end':'s02'}} if mode=='range' else {'source_quote':'Exact source'}
    entries=[
        {'kind':'page_geometry',**evidence,'field':'margin','equals':1.0,'unit':'in'},
        {'kind':'paragraph_words',**evidence,'paragraph_index':0,'min':8,'max':16},
        {'kind':'paragraph_font_size',**evidence,'paragraph_index':0,'equals':12.0},
        {'kind':'paragraph_page_break',**evidence,'paragraph_index':1,'equals':True},
    ]
    call={'name':'word.plan_requirements','arguments':{'source_hash':'a'*64,
          'requirements':[entries[index%4] for index in range(count)]}}
    return '<tool_call>'+json.dumps(call,separators=(',',':'))+'</tool_call>'


@pytest.mark.parametrize('mode', ['range','quote'])
@pytest.mark.parametrize('fixed_hash', [False,True])
@pytest.mark.parametrize('count,accepted', [(0,True),(1,True),(8,True),(9,False),(10,False)])
def test_actual_native_regex_enforces_exact_host_count(mode,fixed_hash,count,accepted):
    tools=tools_for(mode,fixed_hash=fixed_hash)
    original=copy.deepcopy(tools)
    regex=decoding.native_tool_regex(tools)
    assert bool(re.fullmatch(regex,payload(count,mode))) is accepted
    assert tools==original
    assert parameters(tools)['properties']['requirements']['maxItems']==8


@pytest.mark.parametrize('mode', ['range','quote','unprojected'])
@pytest.mark.parametrize('compact', [False,True])
def test_exact_registered_shapes_only_and_no_source_or_string_bound_expansion(mode,compact):
    schema=parameters(tools_for(mode,compact=compact))
    result=decoding.structural_schema(schema)
    assert result['properties']['requirements']['maxItems']==8
    assert 'minItems' not in result['properties']['requirements']
    assert 'pattern' not in result['properties']['source_hash']
    assert 'maxLength' not in json.dumps(result)


@pytest.mark.parametrize('mode', ['range','quote'])
@pytest.mark.parametrize('mutation', ['cap','boolean_cap','extra_property','open_object','missing_required',
    'union_member','nested_bound','boolean_nested','kind','hash_const','hash_extra','extra_definition','evidence'])
def test_tampered_shapes_do_not_receive_object_array_bounds(mode,mutation):
    schema=parameters(tools_for(mode))
    props=schema['properties']
    definition=schema['$defs']['ParagraphWords']
    if mutation=='cap':props['requirements']['maxItems']=9
    elif mutation=='boolean_cap':props['requirements']['maxItems']=True
    elif mutation=='extra_property':props['unexpected']={'type':'string'}
    elif mutation=='open_object':definition['additionalProperties']=True
    elif mutation=='missing_required':definition['required'].remove('min')
    elif mutation=='union_member':props['requirements']['items']['oneOf'].pop()
    elif mutation=='nested_bound':definition['properties']['paragraph_index']['maximum']=999
    elif mutation=='boolean_nested':definition['properties']['paragraph_index']['minimum']=False
    elif mutation=='kind':definition['properties']['kind']['const']='invented'
    elif mutation=='hash_const':props['source_hash']={'const':'invalid'}
    elif mutation=='hash_extra':props['source_hash']={'const':'a'*64,'maxLength':64}
    elif mutation=='extra_definition':schema['$defs']['Extra']={'type':'object'}
    elif mutation=='evidence':
        field='source_range' if mode=='range' else 'source_quote'
        definition['properties'][field]={'type':'object'}
    original=copy.deepcopy(schema)
    assert not decoding._bounded_native_requirements(schema)
    assert 'maxItems' not in decoding.structural_schema(schema)['properties']['requirements']
    assert schema==original


@pytest.mark.parametrize('ids', [[],['s02'],['s01','s03'],['s01','s01'],['s01',2],
                                 [f's{i:02d}' for i in range(1,26)]])
def test_untrusted_range_id_projection_not_bounded(ids):
    schema=parameters(tools_for('range'))
    props=schema['$defs']['NativeRequirementSourceRange']['properties']
    props['start']['enum']=ids
    props['end']['enum']=ids
    assert not decoding._bounded_native_requirements(schema)
    assert 'maxItems' not in decoding.structural_schema(schema)['properties']['requirements']


def test_asymmetric_range_projection_not_bounded():
    schema=parameters(tools_for('range'))
    schema['$defs']['NativeRequirementSourceRange']['properties']['end']['enum']=['s01']
    assert not decoding._bounded_native_requirements(schema)
    assert 'maxItems' not in decoding.structural_schema(schema)['properties']['requirements']


def test_tampered_schema_still_uses_existing_unbounded_object_array_grammar():
    tools=tools_for('quote')
    parameters(tools)['$defs']['ParagraphWords']['properties']['paragraph_index']['maximum']=999
    assert re.fullmatch(decoding.native_tool_regex(tools),payload(10,'quote'))


@pytest.mark.parametrize('operations', [[],['format_table'],['create'],['revise_creation'],
                                       ['edit_creation_blocks'],['plan_content']])
def test_other_native_and_host_schemas_unchanged(operations,monkeypatch):
    host=project_schema(WORD_ACTION.json_schema(),operations)
    tools=compact_tools(action_tools(host))
    before_host=decoding.structural_schema(host)
    before_native=[decoding.structural_schema(tool['function']['parameters']) for tool in tools]
    monkeypatch.setattr(decoding,'_bounded_native_requirements',lambda value:False)
    assert decoding.structural_schema(host)==before_host
    assert [decoding.structural_schema(tool['function']['parameters']) for tool in tools]==before_native


def test_full_host_requirements_schema_keeps_existing_projection(monkeypatch):
    host=project_schema(WORD_ACTION.json_schema(),['plan_requirements'])
    before=decoding.structural_schema(host)
    monkeypatch.setattr(decoding,'_bounded_native_requirements',lambda value:False)
    assert decoding.structural_schema(host)==before


@pytest.mark.parametrize('mode,segments', [('quote',2),('range',2),('range',24)])
def test_offline_regex_size_and_build_measurement(mode,segments,monkeypatch):
    tools=tools_for(mode,fixed_hash=True,segments=segments)
    started=time.perf_counter()
    bounded=decoding.native_tool_regex(tools)
    build_seconds=time.perf_counter()-started
    re.purge()
    started=time.perf_counter()
    compiled=re.compile(bounded)
    compile_seconds=time.perf_counter()-started
    with monkeypatch.context() as patch:
        patch.setattr(decoding,'_bounded_native_requirements',lambda value:False)
        unbounded=decoding.native_tool_regex(tools)
    print(json.dumps({'mode':mode,'segments':segments,'bounded_regex_bytes':len(bounded.encode()),
                      'unbounded_regex_bytes':len(unbounded.encode()),'build_seconds':build_seconds,
                      'python_compile_seconds':compile_seconds}))
    # Regression guard against textual expansion, not a tokenizer-index budget.
    assert len(bounded.encode())<20_000
    assert len(bounded.encode())<=len(unbounded.encode())*1.1
    assert compiled.fullmatch(payload(8,mode))
    assert not compiled.fullmatch(payload(10,mode))
