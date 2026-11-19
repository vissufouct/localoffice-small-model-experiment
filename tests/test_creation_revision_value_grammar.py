"""Group unrestricted revision values without changing Outlines' JSON language."""
import copy
import json
import re

import pytest
from outlines_core.json_schema import build_regex_from_schema

from localoffice.decoding import native_tool_regex, structural_schema
from localoffice.native_tools import action_tools, compact_tools
from localoffice.word.contracts import ReviseCreation

REF = 'cr_' + 'a' * 32


def tools():
    result = action_tools(ReviseCreation.model_json_schema())
    result.append({'type': 'function', 'function': {'name': 'ask_user', 'parameters': {
        'type': 'object', 'properties': {'text': {'type': 'string'}},
        'required': ['text'], 'additionalProperties': False}}})
    return compact_tools(result)


@pytest.fixture(scope='module')
def regex():
    return native_tool_regex(tools())


def matches(regex, changes, *, ensure_ascii=False):
    call = {'name': 'word.revise_creation', 'arguments': {'ref': REF, 'changes': changes}}
    return re.fullmatch(regex, '<tool_call>' + json.dumps(
        call, ensure_ascii=ensure_ascii, separators=(',', ':')) + '</tool_call>') is not None


@pytest.mark.parametrize('value', [False, True, 'hello', 1440, None, {'x': 1}, [1, 'x'],
                                  -1.25, 'Olá 世界', 'quote" slash\\ newline\n tab\t'])
def test_actual_native_regex_accepts_json_value_types(regex, value):
    assert matches(regex, [{'op': 'set', 'path': 'blocks.1.text', 'value': value}])


def test_remove_omission_and_native_array_bounds(regex):
    change = {'op': 'remove', 'path': 'blocks.3'}
    assert matches(regex, [change])
    assert matches(regex, [change] * 4)
    assert not matches(regex, [])
    assert not matches(regex, [change] * 5)
    assert matches(regex, [{'op': 'insert', 'path': 'blocks.1', 'value': {'kind': 'paragraph', 'text': 'Draft'}}])


@pytest.mark.parametrize('kind', ['object', 'array', 'mixed'])
@pytest.mark.parametrize('depth', [1, 2, 3, 4, 5])
def test_grouping_preserves_backend_nesting_language_and_known_limit(regex, kind, depth):
    value = 'leaf'
    for n in range(depth):
        value = {'x': value} if kind == 'object' or (kind == 'mixed' and n % 2) else [value]
    standalone = build_regex_from_schema('{}', whitespace_pattern='[ ]?')
    backend_accepts = re.fullmatch(standalone, json.dumps(value, separators=(',', ':'))) is not None
    assert backend_accepts == (depth <= 3)  # Existing backend limit, not host depth 32.
    assert matches(regex, [{'op': 'set', 'path': 'blocks.1', 'value': value}]) == backend_accepts


def test_existing_unicode_escape_limitation_is_not_claimed_fixed(regex):
    change = {'op': 'set', 'path': 'blocks.1.text', 'value': 'Olá 世界'}
    assert matches(regex, [change], ensure_ascii=False)
    assert not matches(regex, [change], ensure_ascii=True)
    standalone = build_regex_from_schema('{}', whitespace_pattern='[ ]?')
    assert re.fullmatch(standalone, json.dumps(change['value'], ensure_ascii=True)) is None


def test_projection_is_local_and_does_not_mutate_schema():
    params = tools()[0]['function']['parameters']
    original = copy.deepcopy(params)
    projected = structural_schema(params)
    assert projected['$defs']['CreationChange']['properties']['value'] == {'anyOf': [{}]}
    assert projected['properties']['changes']['maxItems'] == 4
    assert params == original
    assert structural_schema({'type': 'object', 'properties': {'value': {}}})['properties']['value'] == {}
    # Nearly matching contracts do not acquire the trusted transformation.
    params['properties']['changes']['maxItems'] = 5
    assert structural_schema(params)['$defs']['CreationChange']['properties']['value'] == {}
    params = copy.deepcopy(original)
    params['$defs']['CreationChange']['properties']['value'] = {'type': 'string'}
    assert structural_schema(params)['$defs']['CreationChange']['properties']['value'] == {'type': 'string'}
