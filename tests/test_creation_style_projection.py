"""Exact explicit-ID native projections; offline grammar and host checks only."""
import copy
import json
import re

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError
from localoffice.native_tools import action_tools, compact_tools, creation_state_tools, validate_native_arguments
from localoffice.word.contracts import EditCreationStyles
from localoffice.word.draft_context import build

REF = 'cr_' + 'a' * 32
TOOL = 'word.edit_creation_styles'
PROPOSAL = {'ref': REF, 'status': 'prepared'}


def inventory(ids=()):
    return {'version': 1, 'ref': REF, 'payload_hash': 'b' * 64, 'status': 'verified',
            'complete': True, 'count': len(ids), 'ids': list(ids)}


def tools(compact=True):
    result = action_tools(EditCreationStyles.model_json_schema()) + [build('Draft', 'df_' + 'c' * 32)[1][1]]
    result.append({'type': 'function', 'function': {'name': 'word.unrelated', 'parameters': {
        'type': 'object', 'additionalProperties': False, 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}})
    return compact_tools(result) if compact else result


def parameters(selected):
    return selected[0]['function']['parameters']


def project(original, evidence, proposal=PROPOSAL):
    before = copy.deepcopy((original, evidence, proposal))
    result = creation_state_tools(original, proposal, explicit_styles=evidence)
    assert (original, evidence, proposal) == before
    assert result[1:] == original[1:]
    Draft202012Validator.check_schema(parameters(result))
    return result


def raw(changes):
    return '<tool_call>' + json.dumps({'name': TOOL, 'arguments': {'ref': REF, 'changes': changes}},
                                     separators=(',', ':')) + '</tool_call>'


def declare(name='ArbitraryNewStyle'):
    return {'op': 'declare', 'id': name, 'font': 'Georgia', 'size_points': 12.0}


@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('status', ['prepared', 'missing_styles', 'invalid_design'])
def test_verified_empty_removes_only_update_branch_and_preserves_contract(compact, status):
    original = tools(compact)
    result = project(original, inventory(), {'ref': REF, 'status': status})
    schema = parameters(result)
    items = schema['properties']['changes']['items']
    assert items['oneOf'] == [{'$ref': '#/$defs/DeclareCreationStyle'}]
    assert 'UpdateCreationStyle' not in schema['$defs']
    if not compact:
        assert items['discriminator'] == {'propertyName': 'op', 'mapping': {'declare': '#/$defs/DeclareCreationStyle'}}
    assert schema['$defs']['DeclareCreationStyle'] == parameters(original)['$defs']['DeclareCreationStyle']
    assert schema['properties']['changes']['minItems'] == 1
    assert schema['properties']['changes']['maxItems'] == 4
    assert schema['properties']['ref'] == {'const': REF}
    regex = native_tool_regex(result)
    for changes in ([declare()], [declare('S' + str(i)) for i in range(4)]):
        assert re.fullmatch(regex, raw(changes))
        validate_native_arguments(TOOL, {'ref': REF, 'changes': changes}, result)
    update = [{'op': 'update', 'id': 'Normal', 'bold': False}]
    assert not re.fullmatch(regex, raw(update))
    with pytest.raises(HarnessError): validate_native_arguments(TOOL, {'ref': REF, 'changes': update}, result)
    for changes in ([], [declare('S' + str(i)) for i in range(5)]):
        with pytest.raises(HarnessError): validate_native_arguments(TOOL, {'ref': REF, 'changes': changes}, result)


@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('ids', [['Normal'], ['Body', 'body'], ['S' + str(i) for i in range(16)]])
def test_complete_nonempty_exact_enum_and_arbitrary_declarations(compact, ids):
    original = tools(compact)
    result = project(original, inventory(ids))
    schema = parameters(result)
    assert schema['$defs']['UpdateCreationStyle']['properties']['id']['enum'] == ids
    assert schema['$defs']['DeclareCreationStyle'] == parameters(original)['$defs']['DeclareCreationStyle']
    expected = copy.deepcopy(parameters(original)['$defs']['UpdateCreationStyle'])
    expected['properties']['id']['enum'] = ids
    assert schema['$defs']['UpdateCreationStyle'] == expected
    assert schema['properties']['changes'] == parameters(original)['properties']['changes']
    regex = native_tool_regex(result)
    for style_id in ids:
        changes = [{'op': 'update', 'id': style_id, 'bold': False}]
        assert re.fullmatch(regex, raw(changes))
        validate_native_arguments(TOOL, {'ref': REF, 'changes': changes}, result)
    changes = [{'op': 'update', 'id': 'NotDeclared', 'bold': False}]
    assert not re.fullmatch(regex, raw(changes))
    with pytest.raises(HarnessError): validate_native_arguments(TOOL, {'ref': REF, 'changes': changes}, result)
    assert re.fullmatch(regex, raw([declare('NotDeclared')]))
    validate_native_arguments(TOOL, {'ref': REF, 'changes': [declare('NotDeclared')]}, result)


@pytest.mark.parametrize('change', [
    None, {}, {'complete': True},
    {'version': True}, {'version': 2}, {'status': 'unavailable'}, {'status': 'ambiguous'},
    {'complete': False}, {'complete': 1}, {'ref': 'cr_' + 'f' * 32}, {'ref': None},
    {'payload_hash': None}, {'payload_hash': 'bad'}, {'payload_hash': 'B' * 64},
    {'count': True}, {'count': -1}, {'count': 17}, {'count': 0.0}, {'count': 1},
    {'ids': None}, {'ids': {}}, {'ids': ['Normal']},
    {'ids': ['Normal', 'Normal'], 'count': 2}, {'ids': [True], 'count': 1},
    {'ids': ['bad id'], 'count': 1}, {'ids': ['1Wrong'], 'count': 1},
    {'ids': ['A' * 65], 'count': 1}, {'ids': ['Normal\n'], 'count': 1},
    {'ids': ['S' + str(i) for i in range(17)], 'count': 17},
    {'reason': 'ambiguous'},
])
def test_missing_incomplete_or_malformed_inventory_never_means_empty(change):
    evidence = change if change is None or change == {} or change == {'complete': True} else {**inventory(), **change}
    original = tools()
    baseline = creation_state_tools(original, PROPOSAL)
    assert project(original, evidence) == baseline


@pytest.mark.parametrize('status', ['stale', 'finalized', None])
def test_ineligible_proposal_never_projects(status):
    original = tools()
    assert project(original, inventory(), {'ref': REF, 'status': status}) == original


@pytest.mark.parametrize('case', ['batch_limit', 'declare_required', 'update_extra', 'union', 'mapping',
                                  'id_pattern', 'font_bound', 'unexpected_definition', 'ref_pattern'])
def test_verified_inventory_requires_exact_registered_schema(case):
    original = tools(False)
    schema = parameters(original)
    if case == 'batch_limit': schema['properties']['changes']['maxItems'] = 1
    elif case == 'declare_required': schema['$defs']['DeclareCreationStyle']['required'].remove('font')
    elif case == 'update_extra': schema['$defs']['UpdateCreationStyle']['additionalProperties'] = True
    elif case == 'union': schema['properties']['changes']['items']['oneOf'].pop()
    elif case == 'mapping': schema['properties']['changes']['items']['discriminator']['mapping']['update'] = '#/$defs/DeclareCreationStyle'
    elif case == 'id_pattern': schema['$defs']['UpdateCreationStyle']['properties']['id']['pattern'] = '.*'
    elif case == 'font_bound': schema['$defs']['DeclareCreationStyle']['properties']['font']['maxLength'] = 999
    elif case == 'ref_pattern': schema['properties']['ref']['pattern'] = '.*'
    else: schema['$defs']['Unexpected'] = {'type': 'string'}
    before = copy.deepcopy(original)
    with pytest.raises(HarnessError) as error:
        creation_state_tools(original, PROPOSAL, explicit_styles=inventory())
    assert error.value.code == 'native_schema' and original == before


def test_duplicate_id_host_guard_remains_and_no_single_change_narrowing():
    selected = project(tools(), inventory(['Normal']))
    changes = [declare('Normal'), {'op': 'update', 'id': 'Normal', 'size_points': 12.0}]
    validate_native_arguments(TOOL, {'ref': REF, 'changes': changes}, selected)
    assert re.fullmatch(native_tool_regex(selected), raw(changes))
    with pytest.raises(ValidationError): EditCreationStyles(ref=REF, changes=changes)


def test_fresh_projection_rebuilds_after_declaration_without_mutating_old_schema():
    original = tools()
    empty = project(original, inventory())
    new_ref = 'cr_' + 'd' * 32
    populated = project(original, {**inventory(['Normal']), 'ref': new_ref, 'payload_hash': 'e' * 64},
                        {'ref': new_ref, 'status': 'prepared'})
    assert 'UpdateCreationStyle' not in parameters(empty)['$defs']
    assert parameters(populated)['properties']['ref'] == {'const': new_ref}
    assert parameters(populated)['$defs']['UpdateCreationStyle']['properties']['id']['enum'] == ['Normal']


def test_matching_prebound_ref_and_missing_style_tool_are_supported():
    original = tools()
    parameters(original)['properties']['ref'] = {'const': REF}
    assert 'UpdateCreationStyle' not in parameters(project(original, inventory()))['$defs']
    unrelated = original[1:]
    assert creation_state_tools(unrelated, PROPOSAL, explicit_styles=inventory()) == unrelated
