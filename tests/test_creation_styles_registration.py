"""Style-tool registration and lazy exposure; no runtime routing or inference."""
import copy
import json
import re

import pytest
from jsonschema import Draft202012Validator

from localoffice.errors import HarnessError
from localoffice.decoding import native_tool_regex
from localoffice.native_tools import action_tools, compact_tools, validate_native_arguments
from localoffice.plugins import first_party_registry
from localoffice.runtime import WORD_ACTION
from localoffice.storage import ProjectStore
from localoffice.word import contracts, creation_styles, guidance
from localoffice.word.plugin import WordPlugin

TOOL = 'word.edit_creation_styles'
REF = 'cr_' + 'a' * 32


@pytest.fixture
def store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Style registration must not invoke a document worker.')
    monkeypatch.setattr(WordPlugin, '_execute', forbidden)
    instance = ProjectStore(tmp_path)
    yield instance
    assert instance.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert not (tmp_path / 'report.docx').exists()
    instance.close()


def full_schema():
    return WORD_ACTION.json_schema()


def assert_local_refs(schema):
    Draft202012Validator.check_schema(schema)
    def visit(value):
        if isinstance(value, dict):
            if '$ref' in value:
                assert value['$ref'].startswith('#/$defs/')
                assert value['$ref'].split('/')[-1] in schema['$defs']
            for child in value.values():visit(child)
        elif isinstance(value, list):
            for child in value:visit(child)
    visit(schema)


@pytest.mark.asyncio
async def test_registry_async_wrapper_forwards_normalized_sparse_changes_and_identity(store, monkeypatch):
    supplied = {'ref': REF, 'changes': [{'op': 'update', 'id': 'Body', 'bold': False}]}
    normalizer = creation_styles.normalize
    normalized = normalizer(supplied)
    calls = []
    receipt = {'ref': 'cr_' + 'b' * 32, 'status': 'prepared', 'document_changed': False}

    def normalize(arguments):
        calls.append(('normalize', copy.deepcopy(arguments)))
        return normalizer(arguments)

    def edit(actual_store, task_id, **arguments):
        assert actual_store is store
        calls.append(('edit', task_id, copy.deepcopy(arguments)))
        return receipt

    monkeypatch.setattr(creation_styles, 'normalize', normalize)
    monkeypatch.setattr(creation_styles, 'edit', edit)
    registry = first_party_registry(store)
    assert TOOL in registry.plugins['word'].capabilities
    assert registry.tools[TOOL] == 'word'
    asynchronous = registry.word.call_async
    async_calls = []

    async def tracked(name, **arguments):
        async_calls.append(name)
        return await asynchronous(name, **arguments)

    monkeypatch.setattr(registry.word, 'call_async', tracked)
    call_id = 'call_' + 'c' * 32
    result = await registry.call(TOOL, task_id='task', native_call_id=call_id, **supplied)
    assert result == receipt and async_calls == ['edit_creation_styles']
    assert calls == [('normalize', supplied), ('edit', 'task', {
        'ref': REF, 'changes': normalized['changes'], 'native_call_id': call_id})]
    assert registry.word.busy is False
    assert normalized['changes'] == supplied['changes']


@pytest.mark.asyncio
async def test_disabled_plugin_does_not_normalize_or_dispatch(store, monkeypatch):
    def forbidden(*args, **kwargs):pytest.fail('Disabled plugin dispatched.')
    monkeypatch.setattr(creation_styles, 'normalize', forbidden)
    monkeypatch.setattr(creation_styles, 'edit', forbidden)
    registry = first_party_registry(store)
    registry.set_enabled('word', False)
    with pytest.raises(HarnessError) as exc:
        await registry.call(TOOL, task_id='task', ref=REF, changes=[])
    assert exc.value.code == 'plugin_disabled'


@pytest.mark.asyncio
async def test_normalization_error_prevents_dispatch_and_releases_busy_flag(store, monkeypatch):
    def rejected(*args, **kwargs):raise HarnessError('invalid_schema', 'Rejected style input')
    def forbidden(*args, **kwargs):pytest.fail('Invalid style input dispatched.')
    monkeypatch.setattr(creation_styles, 'normalize', rejected)
    monkeypatch.setattr(creation_styles, 'edit', forbidden)
    registry = first_party_registry(store)
    with pytest.raises(HarnessError, match='Rejected style input'):
        await registry.call(TOOL, task_id='task', ref=REF, changes=[])
    assert registry.word.busy is False


def test_lazy_style_contract_is_direct_and_not_implicitly_exposed():
    assert guidance.MODELS['edit_creation_styles'] is contracts.EditCreationStyles
    assert guidance.DIRECT_MODELS['edit_creation_styles'] is contracts.EditCreationStyles
    state = guidance.build_state(['edit_creation_styles'])
    assert state['operations'] == ['edit_creation_styles']
    assert state['guidance'] == {'edit_creation_styles': guidance.GUIDES['edit_creation_styles']}
    assert contracts.LoadTools.model_json_schema()['properties']['operations']['maxItems'] == 4
    assert TOOL not in guidance.prompt_schemas(['revise_creation'])
    projected = guidance.project_schema(full_schema(), ['revise_creation'])
    assert 'EditCreationStyles' not in projected['$defs']
    assert TOOL not in projected['discriminator']['mapping']


@pytest.mark.parametrize('names', [['edit_creation_styles'],
    ['edit_creation_styles', 'edit_creation_blocks', 'finalize_creation'],
    ['edit_creation_styles', 'plan_requirements']])
def test_proposal_only_selection_has_direct_schema_and_no_stage_batch(names):
    prompts = guidance.prompt_schemas(names)
    assert TOOL in prompts and 'word.stage_batch' not in prompts
    for schema in prompts.values():assert_local_refs(schema)
    full = full_schema()
    original = copy.deepcopy(full)
    projected = guidance.project_schema(full, names)
    assert full == original
    assert_local_refs(projected)
    assert 'EditCreationStyles' in projected['$defs']
    assert 'StageBatch' not in projected['$defs']


@pytest.mark.parametrize('operation,model', [('format_table', 'FormatTable'), ('set_list', 'SetList')])
def test_mixed_selection_never_includes_style_proposal_tool_in_document_batch(operation, model):
    names = ['edit_creation_styles', operation]
    prompts = guidance.prompt_schemas(names)
    projected = guidance.project_schema(full_schema(), names)
    assert TOOL in prompts
    for batch in (prompts['word.stage_batch'], projected['$defs']['StageBatch']):
        assert batch['properties']['operations']['items']['oneOf'] == [{'$ref': '#/$defs/' + model}]
        assert batch['properties']['operations']['maxItems'] == 24
    assert 'EditCreationStyles' not in prompts['word.stage_batch']['$defs']
    assert_local_refs(projected)


@pytest.mark.parametrize('changes', [
    [{'op': 'declare', 'id': 'Body', 'font': 'Arial', 'size_points': 12.0}],
    [{'op': 'update', 'id': 'Body', 'bold': False}],
    [{'op': 'update', 'id': 'Body', 'size_points': 12.5, 'italic': False}],
    [{'op': 'declare', 'id': 'Body', 'font': 'Arial', 'size_points': 12.0},
     {'op': 'update', 'id': 'Normal', 'bold': False}],
])
def test_actual_native_grammar_accepts_declare_update_omissions_and_false(changes):
    tools = compact_tools(action_tools(guidance.project_schema(full_schema(), ['edit_creation_styles'])))
    tools = [tool for tool in tools if tool['function']['name'] in (TOOL, 'ask_user')]
    arguments = {'ref': REF, 'changes': changes}
    raw = '<tool_call>' + json.dumps({'name': TOOL, 'arguments': arguments}, separators=(',', ':')) + '</tool_call>'
    assert re.fullmatch(native_tool_regex(tools), raw)
    validate_native_arguments(TOOL, arguments, tools)
    normalized = creation_styles.normalize(arguments)
    assert normalized['changes'] == changes
    assert normalized['ref'] == REF
    # No assertion that the structural grammar enforces object-array count caps.
    assert contracts.EditCreationStyles.model_json_schema()['properties']['changes']['maxItems'] == 4


@pytest.mark.asyncio
async def test_real_registry_declaration_and_sparse_update_preserve_other_content(store):
    from localoffice.chats import current_chat
    from localoffice.native_history import record_call, record_result
    from localoffice.word.creation_repair import get
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('task', 'Use a named style.', 'running', 'background', 'report.docx', 1, 1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(store)))
    registry = first_party_registry(store)
    proposal = registry.word.prepare_creation(task_id='task', path='report.docx',
                                               blocks=[{'kind': 'paragraph', 'text': 'Exact café 😀'}])
    before = copy.deepcopy(get(store, 'task')['payload'])
    for changes in ([{'op': 'declare', 'id': 'Normal', 'font': 'Arial', 'size_points': 12.0, 'bold': True}],
                    [{'op': 'update', 'id': 'Normal', 'bold': False}]):
        args = {'ref': proposal['ref'], 'changes': changes}
        call_id = record_call(store, 'task', {'tool': TOOL, **args})
        proposal = await registry.call(TOOL, task_id='task', native_call_id=call_id, **args)
        assert creation_styles.result(store, 'task', args, native_call_id=call_id) == proposal
        record_result(store, 'task', call_id, proposal)
        state = get(store, 'task')['payload']
        assert {k: v for k, v in state.items() if k != 'styles'} == {k: v for k, v in before.items() if k != 'styles'}
    style = get(store, 'task')['payload']['styles'][0]
    assert style['id'] == 'Normal' and style['font'] == 'Arial'
    assert style['size_points'] == 12.0 and style['bold'] is False
