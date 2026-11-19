"""Native root/syntax restrictions preserve generic revision values and roots."""
import copy
import json
import re

import pytest

from localoffice.decoding import native_tool_regex, structural_schema
from localoffice.native_tools import action_tools, compact_tools, creation_revision_path_pattern
from localoffice.word.contracts import ReviseCreation
from localoffice.word.contracts import PageSettings
from localoffice.word.creation_revision import ROOTS


def tools():
    return compact_tools(action_tools(ReviseCreation.model_json_schema()) + [{
        'type': 'function', 'function': {'name': 'ask_user', 'parameters': {
            'type': 'object', 'properties': {'text': {'type': 'string'}}}}}])


@pytest.fixture(scope='module')
def regex():
    return native_tool_regex(tools())


def matches(regex, path, value='Exact Δ'):
    call = {'name': 'word.revise_creation', 'arguments': {
        'ref': 'cr_' + 'a' * 32, 'changes': [{'op': 'set', 'path': path, 'value': value}]}}
    return re.fullmatch(regex, '<tool_call>' + json.dumps(
        call, ensure_ascii=False, separators=(',', ':')) + '</tool_call>') is not None


@pytest.mark.parametrize('root', sorted(ROOTS))
def test_every_host_root_is_exposed(regex, root):
    assert matches(regex, root)
    assert matches(regex, root + '.arbitrary_child.0') == (root!='page')


@pytest.mark.parametrize('field', sorted(PageSettings.model_fields))
def test_every_page_field_is_exposed(regex,field):
    assert matches(regex,'page.'+field,1440)
    assert not matches(regex,'page.'+field+'.extra',1440)


@pytest.mark.parametrize('path',['page.margin','page.height','page.width','page.0','page.margin_twips_extra'])
def test_unknown_page_fields_rejected(regex,path):
    assert not matches(regex,path,1440)


@pytest.mark.parametrize('path', ['blocks.0.text', 'blocks.23.rows.0.1', 'page.margin_twips',
    'styles.0.size_points', 'first_header.text', 'literal_refs.blocks.0.text', 'blocks.123456.text'])
def test_nested_paths_and_canonical_indices(regex, path):
    assert matches(regex, path)


@pytest.mark.parametrize('path', ['block_layout', 'block_layout.paragraphs', 'block_layout.page_break_before',
    'block_layout.block_count', 'proposed_design', 'proposed_design.items.0', 'proposed_literals',
    'tool', 'path', 'revision', 'blocks.', '.blocks', 'blocks..text', 'blocks.-1.text',
    'blocks.01.text', 'blocks.00', 'blocks.+1', 'blocks.١.text', 'blocks.-text', '', 'blocksX.text'])
def test_summary_protected_roots_and_bad_segments_rejected(regex, path):
    assert not matches(regex, path)


@pytest.mark.parametrize('value', [False, True, 'text', 1440, None, {'x': 1}, [1, 'x']])
def test_all_json_value_types_survive_root_constraint(regex, value):
    assert matches(regex, 'blocks', value)


def test_only_exact_trusted_path_projection_is_retained():
    p = tools()[0]['function']['parameters']
    original = copy.deepcopy(p)
    pattern = creation_revision_path_pattern()
    assert structural_schema(p)['$defs']['CreationChange']['properties']['path']['pattern'] == pattern
    assert p == original
    assert 'pattern' not in ReviseCreation.model_json_schema()['$defs']['CreationChange']['properties']['path']
    p['$defs']['CreationChange']['properties']['path']['pattern'] = '^blocks$'
    projected = structural_schema(p)
    assert 'pattern' not in projected['$defs']['CreationChange']['properties']['path']
    assert 'maxItems' not in projected['properties']['changes']
    assert 'pattern' not in structural_schema({'type': 'string', 'pattern': pattern})
