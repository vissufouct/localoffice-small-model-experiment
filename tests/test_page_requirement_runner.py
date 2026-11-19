"""Actual runner schema assembly; tokenizer stub only, never model inference."""
import copy
import json
import re

import pytest

from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError
from localoffice.native_tools import validate_native_arguments
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical, digest
from localoffice.word.guidance import build_state
from localoffice.word.requirements import plan
from test_native_runner import NativeProvider
from test_word_content_plan import fixture

OBJECTIVE = 'Create a document. Use one-inch margins. Keep the supplied text.'


class AssemblyOnlyProvider(NativeProvider):
    async def stream(self, *args, **kwargs):
        pytest.fail('These assembly tests must not perform inference.')
        yield  # Keep the provider interface an async generator.


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    store = fixture(tmp_path, OBJECTIVE)
    providers = []

    def forbidden(*args, **kwargs):
        pytest.fail('Schema assembly must not run document workers.')

    monkeypatch.setattr('localoffice.word.plugin.WordPlugin._execute', forbidden)

    def runner(*, enabled=True):
        provider = AssemblyOnlyProvider(None)
        providers.append(provider)
        return TaskRunner(store, provider, source_planning=True, creation_review=True,
                          requirements_planning=enabled)

    yield store, runner
    assert all(provider.calls == 0 for provider in providers)
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM native_calls').fetchone()[0] == 0
    assert not (store.root / 'report.docx').exists()
    store.close()


def margin(value=1.0):
    return {'kind': 'page_geometry', 'source_range': {'start': 's02', 'end': 's02'},
            'field': 'margin', 'equals': value, 'unit': 'in'}


def selected_plan(store, entries):
    plan(store, 'task', source_hash=digest(OBJECTIVE.encode()), requirements=entries)


def preparation(runner):
    return next(tool for tool in runner.native_tools if tool['function']['name'] == 'word.prepare_creation')


def payload(page):
    arguments = {'path': 'report.docx'}
    if page is not None:arguments['page'] = page
    arguments['blocks'] = [{'kind': 'paragraph', 'text': 'Body'}]
    return arguments


def matches(regex, arguments):
    return bool(re.fullmatch(regex, '<tool_call>' + json.dumps(
        {'name': 'word.prepare_creation', 'arguments': arguments}, separators=(',', ':')) + '</tool_call>'))


@pytest.mark.asyncio
async def test_runner_exposes_required_selected_margin_in_actual_native_tool(ctx):
    store, make_runner = ctx
    selected_plan(store, [margin()])
    runner = make_runner()
    await runner.assemble('task', word_tool_override=build_state(['prepare_creation']))
    tool = preparation(runner)
    schema = tool['function']['parameters']
    page = schema['$defs']['PageSettings']
    assert 'page' in schema['required']
    assert page['properties']['margin_twips']['const'] == 1440
    assert 'margin_twips' in page['required']
    assert 'width_twips' not in page['required'] and 'height_twips' not in page['required']
    assert runner.page_requirement_stamp['status'] == 'ready'
    assert runner.page_requirement_stamp['fields'] == {'margin_twips': 1440}
    assert runner.provider.counted[1] == runner.native_tools
    tools = [tool, next(t for t in runner.native_tools if t['function']['name'] == 'ask_user')]
    regex = native_tool_regex(tools)
    valid = payload({'margin_twips': 1440})
    assert matches(regex, valid)
    validate_native_arguments('word.prepare_creation', valid, runner.native_tools)
    for invalid in (payload({'margin_twips': 2880}), payload({}), payload(None)):
        assert not matches(regex, invalid)
        with pytest.raises(HarnessError):
            validate_native_arguments('word.prepare_creation', invalid, runner.native_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize('entries', [[margin()], [margin(), margin(2.0)], None])
async def test_opt_out_keeps_unprojected_preparation_with_or_without_plan(ctx, entries):
    store, make_runner = ctx
    runner = make_runner(enabled=False)
    await runner.assemble('task', word_tool_override=build_state(['prepare_creation']))
    baseline = copy.deepcopy(preparation(runner))
    if entries is not None:selected_plan(store, entries)
    await runner.assemble('task', word_tool_override=build_state(['prepare_creation']))
    assert preparation(runner) == baseline
    assert runner.page_requirement_stamp is None
    schema = baseline['function']['parameters']
    assert 'page' not in schema['required']
    assert 'const' not in schema['$defs']['PageSettings']['properties']['margin_twips']
    validate_native_arguments('word.prepare_creation', payload(None), runner.native_tools)
    validate_native_arguments('word.prepare_creation', payload({'margin_twips': 2880}), runner.native_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize('entries', [[], [{'kind': 'paragraph_words', 'source_quote': 'Keep the supplied text',
                                          'paragraph_index': 0, 'min': 1, 'max': 12}]])
async def test_fresh_empty_or_non_page_plan_leaves_native_preparation_unchanged(ctx, entries):
    store, make_runner = ctx
    selected_plan(store, entries)
    ordinary = make_runner(enabled=False)
    enabled = make_runner()
    selection = build_state(['prepare_creation'])
    await ordinary.assemble('task', word_tool_override=selection)
    await enabled.assemble('task', word_tool_override=selection)
    assert preparation(enabled) == preparation(ordinary)
    assert enabled.page_requirement_stamp['status'] == 'ready'
    assert enabled.page_requirement_stamp['fields'] == {}
    assert enabled.page_requirement_stamp['generation'] == 1
    assert enabled.page_requirement_stamp['plan_hash']
    validate_native_arguments('word.prepare_creation', payload(None), enabled.native_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['missing', 'stale', 'conflict'])
@pytest.mark.parametrize('operations', [['prepare_creation'], ['prepare_creation', 'plan_requirements']])
async def test_unavailable_preparation_retains_planning_and_question_exits(ctx, state, operations):
    store, make_runner = ctx
    if state == 'conflict':selected_plan(store, [margin(), margin(2.0)])
    elif state == 'stale':
        selected_plan(store, [margin()])
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective='Changed objective.' WHERE id='task'")
    runner = make_runner()
    messages = await runner.assemble('task', word_tool_override=build_state(operations))
    names = {tool['function']['name'] for tool in runner.native_tools}
    assert 'word.prepare_creation' not in names
    assert 'finish' not in names
    assert {'word.load_tools', 'ask_user'} <= names
    validate_native_arguments('ask_user', {'text': 'Which conflicting requirement should apply?'}, runner.native_tools)
    validate_native_arguments('word.load_tools', {'operations': ['plan_requirements']}, runner.native_tools)
    if state != 'conflict' or 'plan_requirements' in operations:
        assert 'word.plan_requirements' in names
    if state == 'conflict':
        stamp = runner.page_requirement_stamp
        assert stamp['status'] == 'conflict'
        assert canonical(stamp['diagnostic']) in messages[0]['content']
        assert 'Load plan_requirements' in messages[0]['content']
    assert runner.provider.counted[1] == runner.native_tools


@pytest.mark.asyncio
async def test_new_assembly_without_preparation_drops_previous_projection_stamp(ctx):
    store, make_runner = ctx
    selected_plan(store, [margin()])
    runner = make_runner()
    await runner.assemble('task', word_tool_override=build_state(['prepare_creation']))
    assert runner.page_requirement_stamp['status'] == 'ready'
    await runner.assemble('task', word_tool_override=build_state(['inspect']))
    assert 'word.prepare_creation' not in {tool['function']['name'] for tool in runner.native_tools}
    assert runner.page_requirement_stamp is None
