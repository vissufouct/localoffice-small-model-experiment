"""Offline authoritative page projections and actual native decoding checks."""
import copy
import json
import re

import pytest

from localoffice.chats import create_chat, current_chat, select_chat
from localoffice.decoding import native_tool_regex
from localoffice.errors import HarnessError
from localoffice.native_tools import action_tools, compact_tools, validate_native_arguments
from localoffice.runtime import WORD_ACTION
from localoffice.storage import ProjectStore, digest
from localoffice.word import requirements
from localoffice.word.guidance import project_schema
from localoffice.word.page_requirement_projection import project_tools, require_fresh, MAX_DIAGNOSTIC_BYTES, MAX_STAMP_BYTES
from localoffice.storage import canonical

TEXT = 'Page instructions café 😀 "quoted" \\ here. Draft eight words.'


@pytest.fixture
def store(tmp_path):
    instance = ProjectStore(tmp_path)
    with instance.transaction():
        instance.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                            ('task', TEXT, 'running', 'background', 'report.docx', 1, 1))
        instance.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(instance)))
    yield instance
    assert instance.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert not (tmp_path / 'report.docx').exists()
    instance.close()


def tools(compact=True):
    result = action_tools(project_schema(WORD_ACTION.json_schema(), ['prepare_creation', 'plan_requirements', 'create']))
    return compact_tools(result) if compact else result


def geometry(field='margin', value=1.0, unit='in', *, ranged=False):
    evidence = {'source_range': {'start': 's01', 'end': 's01'}} if ranged else {'source_quote': 'Page instructions'}
    return {'kind': 'page_geometry', **evidence, 'field': field, 'equals': value, 'unit': unit}


def plan(store, entries):
    requirements.plan(store, 'task', source_hash=digest(TEXT.encode()), requirements=entries)


def prepare(selected):
    return next(tool['function']['parameters'] for tool in selected if tool['function']['name'] == 'word.prepare_creation')


def projection(store, original=None):
    original = tools() if original is None else original
    saved = copy.deepcopy(original)
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    projected, stamp = project_tools(store, 'task', original)
    assert original == saved
    assert store.db.total_changes == changes and list(store.db.iterdump()) == before
    assert len(canonical(stamp).encode()) <= MAX_STAMP_BYTES
    assert len(stamp['diagnostic'].encode()) <= MAX_DIAGNOSTIC_BYTES
    assert stamp['interpretation'] == 'model_selected'
    assert stamp['owner'] == {'task_id': 'task', 'chat_id': current_chat(store), 'target': 'report.docx'}
    if stamp['status'] == 'ready':
        require_fresh(store, 'task', stamp)
    else:
        with pytest.raises(HarnessError):require_fresh(store, 'task', stamp)
    assert store.db.total_changes == changes and list(store.db.iterdump()) == before
    return projected, stamp


@pytest.mark.parametrize('unit,value,expected', [('in', 1.0, 1440), ('cm', 2.54, 1440),
                                              ('pt', 72.0, 1440), ('twip', 1440.0, 1440),
                                              ('twip', 1440.5, 1441)])
@pytest.mark.parametrize('ranged', [False, True])
def test_actual_native_grammar_constants_and_omission_escape(store, unit, value, expected, ranged):
    plan(store, [geometry(value=value, unit=unit, ranged=ranged)])
    projected, stamp = projection(store)
    assert stamp['fields'] == {'margin_twips': expected}
    selected = [tool for tool in projected if tool['function']['name'] in ('word.prepare_creation', 'ask_user')]
    regex = native_tool_regex(selected)

    def matches(arguments):
        raw = '<tool_call>' + json.dumps({'name': 'word.prepare_creation', 'arguments': arguments},
                                        separators=(',', ':')) + '</tool_call>'
        return bool(re.fullmatch(regex, raw))

    # Schema declaration order is also the native grammar's property order.
    valid = {'path': 'report.docx', 'page': {'margin_twips': expected},
             'blocks': [{'kind': 'paragraph', 'text': 'Body'}]}
    assert matches(valid)
    validate_native_arguments('word.prepare_creation', valid, projected)
    for invalid in ({**valid, 'page': {'margin_twips': 2880}}, {**valid, 'page': {}},
                    {key: item for key, item in valid.items() if key != 'page'}):
        assert not matches(invalid)
        with pytest.raises(HarnessError):validate_native_arguments('word.prepare_creation', invalid, projected)


@pytest.mark.parametrize('compact', [False, True])
def test_only_selected_page_properties_and_prepare_tool_change(store, compact):
    plan(store, [geometry(), geometry('width', 8.5), geometry('height', 11.0)])
    original = tools(compact)
    projected, stamp = projection(store, original)
    assert stamp['fields'] == {'height_twips': 15840, 'margin_twips': 1440, 'width_twips': 12240}
    for prior, current in zip(original, projected):
        if prior['function']['name'] != 'word.prepare_creation':
            assert prior == current
    before, after = prepare(original), prepare(projected)
    assert before['properties'] == after['properties']
    assert after['required'] == [*before['required'], 'page']
    for name, schema in before['$defs'].items():
        if name != 'PageSettings':assert schema == after['$defs'][name]
    page_before = before['$defs']['PageSettings']
    page_after = after['$defs']['PageSettings']
    for field, schema in page_before['properties'].items():
        if field in stamp['fields']:
            assert page_after['properties'][field] == {**{k: v for k, v in schema.items() if k != 'default'},
                                                       'const': stamp['fields'][field]}
        else:assert page_after['properties'][field] == schema
    assert set(page_after['required']) == set(stamp['fields'])
    assert prepare(original) is not prepare(projected)
    assert stamp['plan_hash'] == digest(canonical(requirements.get(store, 'task')).encode())


@pytest.mark.parametrize('entries', [[], [{'kind': 'paragraph_words', 'source_quote': 'Draft eight words',
                                          'paragraph_index': 0, 'min': 8, 'max': 16}]])
def test_no_page_constraints_unchanged_but_complete_plan_stamped(store, entries):
    plan(store, entries)
    original = tools()
    projected, stamp = projection(store, original)
    assert stamp['status'] == 'ready' and stamp['fields'] == {}
    assert projected == original and projected is not original
    assert stamp['generation'] == 1 and stamp['plan_hash'] is not None
    assert stamp['source_hash'] == stamp['current_source_hash'] == digest(TEXT.encode())
    plan(store, entries)
    with pytest.raises(HarnessError, match='changed'):require_fresh(store, 'task', stamp)


def test_equivalent_and_rounded_duplicate_values_agree(store):
    plan(store, [geometry(), geometry(value=2.54, unit='cm'), geometry(value=72.0, unit='pt'),
                 geometry(value=1439.6, unit='twip')])
    _, stamp = projection(store)
    assert stamp['status'] == 'ready' and stamp['fields'] == {'margin_twips': 1440}


@pytest.mark.parametrize('entries', [
    [geometry(), geometry(value=2.0)],
    [geometry('width', 4.0), geometry('width', 5.0)],
    [geometry(value=1.0), geometry('width', 2.0)],
    [geometry(value=1.0), geometry('height', 2.0)],
])
def test_conflicting_values_and_selected_usable_area_remove_only_preparation(store, entries):
    plan(store, entries)
    original = tools()
    projected, stamp = projection(store, original)
    assert stamp['status'] == 'conflict' and stamp['fields'] == {}
    assert 'conflict' in stamp['diagnostic'].lower()
    assert projected == [tool for tool in original if tool['function']['name'] != 'word.prepare_creation']
    assert {'ask_user', 'word.load_tools', 'word.plan_requirements'} <= {tool['function']['name'] for tool in projected}


@pytest.mark.parametrize('entries,expected', [
    ([geometry('width', 2.0)], {'width_twips': 2880}),
    ([geometry('height', 2.0)], {'height_twips': 2880}),
    ([geometry(value=3.0)], {'margin_twips': 4320}),
    ([geometry(value=1.0), geometry('width', 3.0)], {'margin_twips': 1440, 'width_twips': 4320}),
])
def test_usable_area_uses_only_explicit_dimensions_and_inclusive_boundary(store, entries, expected):
    plan(store, entries)
    _, stamp = projection(store)
    assert stamp['status'] == 'ready' and stamp['fields'] == expected


@pytest.mark.parametrize('status', ['missing', 'stale'])
def test_missing_or_stale_plan_abstains_with_bounded_diagnostic(store, status):
    if status == 'stale':
        plan(store, [geometry()])
        with store.transaction():store.db.execute("UPDATE tasks SET objective='A new objective' WHERE id='task'")
    original = tools()
    projected, stamp = projection(store, original)
    assert stamp['status'] == status and stamp['fields'] == {}
    assert projected == [tool for tool in original if tool['function']['name'] != 'word.prepare_creation']


@pytest.mark.parametrize('change', ['generation', 'non_page', 'source', 'target', 'chat', 'tamper', 'stamp'])
def test_pre_dispatch_rejects_changed_complete_authority(store, change):
    plan(store, [geometry()])
    _, stamp = projection(store)
    if change == 'generation':plan(store, [geometry()])
    elif change == 'non_page':
        plan(store, [geometry(), {'kind': 'paragraph_words', 'source_quote': 'Draft eight words',
                                 'paragraph_index': 0, 'min': 8, 'max': 16}])
    elif change == 'source':
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    elif change == 'target':
        with store.transaction():store.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
    elif change == 'chat':select_chat(store, create_chat(store, 'Other chat'))
    elif change == 'tamper':
        with store.transaction():
            store.db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', requirements.HISTORY_PREFIX + 'task:1'))
    elif change == 'stamp':stamp['fields']['margin_twips'] = 2880
    before = list(store.db.iterdump())
    with pytest.raises(HarnessError):require_fresh(store, 'task', stamp)
    assert list(store.db.iterdump()) == before
    if change in ('target', 'chat', 'tamper'):
        with pytest.raises(HarnessError):project_tools(store, 'task', tools())


@pytest.mark.parametrize('mutation', ['reference', 'definition', 'extra_property', 'shared_ref', 'open', 'required'])
def test_unexpected_schema_fails_explicitly_without_modification(store, mutation):
    plan(store, [geometry()])
    supplied = tools()
    schema = prepare(supplied)
    if mutation == 'reference':schema['properties']['page'] = {'$ref': '#/$defs/Other'}
    elif mutation == 'definition':schema['$defs']['PageSettings']['properties']['margin_twips']['maximum'] = 9999
    elif mutation == 'extra_property':schema['$defs']['PageSettings']['properties']['extra'] = {'type': 'integer'}
    elif mutation == 'shared_ref':schema['properties']['other'] = {'$ref': '#/$defs/PageSettings'}
    elif mutation == 'open':schema['$defs']['PageSettings']['additionalProperties'] = True
    elif mutation == 'required':schema['required'] = 'page'
    before = copy.deepcopy(supplied)
    with pytest.raises(HarnessError) as exc:project_tools(store, 'task', supplied)
    assert exc.value.code == 'native_schema' and supplied == before


def test_absent_prepare_tool_is_not_added(store):
    plan(store, [geometry()])
    original = [tool for tool in tools() if tool['function']['name'] != 'word.prepare_creation']
    projected, stamp = projection(store, original)
    assert projected == original and stamp['status'] == 'ready'


def test_native_grammar_requires_every_selected_page_field(store):
    plan(store, [geometry('width', 8.5), geometry('height', 11.0), geometry()])
    projected, _ = projection(store)
    selected = [tool for tool in projected if tool['function']['name'] in ('word.prepare_creation', 'ask_user')]
    regex = native_tool_regex(selected)
    page = {'width_twips': 12240, 'height_twips': 15840, 'margin_twips': 1440}

    def matches(value):
        args = {'path': 'report.docx', 'page': value, 'blocks': [{'kind': 'paragraph', 'text': 'Body'}]}
        return re.fullmatch(regex, '<tool_call>' + json.dumps({'name': 'word.prepare_creation', 'arguments': args},
                                                            separators=(',', ':')) + '</tool_call>')

    assert matches(page)
    for field in page:
        assert not matches({key: value for key, value in page.items() if key != field})
        assert not matches({**page, field: page[field] + 1})


def test_returned_tools_and_stamp_are_independent_of_caller_objects(store):
    plan(store, [geometry()])
    original = tools()
    projected, stamp = projection(store, original)
    projected[0]['function']['name'] = 'changed'
    stamp['owner']['target'] = 'changed.docx'
    assert original[0]['function']['name'] != 'changed'
    assert requirements.get(store, 'task')['owner']['target'] == 'report.docx'
    with pytest.raises(HarnessError):require_fresh(store, 'task', stamp)
