"""Presentation-only catalogue experiment; no inference or scope selection."""
import copy
import json
import re

import pytest

from localoffice.storage import canonical
from localoffice.word import draft_planning_context as planning
from localoffice.word.objective_evidence import catalogue_text
from localoffice.native_tools import validate_native_arguments
from test_draft_fragments import ctx
from test_draft_planning_context import Provider, context, tools_for


@pytest.mark.parametrize('source', [
    'Copy "café 😀" exactly. Draft a response; Keep the ending.',
    '  First line\r\nSecond line\nThird\tline with a backslash \\ and "quotes".  ',
    'Combining e\u0301 and Unicode “aspas”.\n日本語とالعربية.',
    'Literal \\n is not a newline. Actual newline\nEnd.',
])
def test_pretty_catalogue_exact_roundtrip_offsets_and_unicode(source):
    evidence = catalogue_text(source)
    assert evidence['available']
    original = copy.deepcopy(evidence)
    rendered = planning.format_catalogue(evidence)
    recovered = json.loads(rendered)
    assert recovered == evidence == original
    assert rendered != canonical(evidence) and '\n' in rendered
    assert ''.join(segment['text'] for segment in recovered['segments'].values()) == source
    for identity, segment in evidence['segments'].items():
        assert source[segment['start']:segment['end']] == recovered['segments'][identity]['text']
        assert f'    "{identity}": {{\n' in rendered
    assert re.findall(r'^    "(s\d\d)": \{$', rendered, re.MULTILINE) == list(evidence['segments'])
    assert '😀' in rendered if '😀' in source else True
    assert '\\u00e9' not in rendered


def test_formatter_preserves_all_metadata_without_filtering():
    evidence = catalogue_text('First. Second.')
    evidence['additional_metadata'] = {'label': 'Unmodified "metadata"\n😀', 'values': [None, False, 0]}
    before = copy.deepcopy(evidence)
    assert json.loads(planning.format_catalogue(evidence)) == before
    assert evidence == before
    unavailable = catalogue_text('x' * 3000)
    assert not unavailable['available']
    assert json.loads(planning.format_catalogue(unavailable)) == unavailable


@pytest.mark.asyncio
async def test_context_changes_only_catalogue_presentation_and_writes_nothing(ctx, monkeypatch):
    source = 'Copy "café 😀".\nDraft an explanation with \\ and "quotes"; Keep the ending.'
    with ctx.transaction():
        ctx.db.execute('UPDATE tasks SET objective=? WHERE id=?', (source, 'task'))
    ctx.message('user', source, 'task')
    ctx.message('user', 'Use a neutral tone.\nKeep accents.', 'task')
    decisions = [{'scope': 'task', 'instruction': 'Preserve Unicode 😀.'}]
    tools = tools_for(ctx)
    before_tools = copy.deepcopy(tools)
    before_system = planning.SYSTEM
    before_database = list(ctx.db.iterdump())
    before_changes = ctx.db.total_changes
    provider = Provider()
    messages, selected, info = await context(ctx, provider, decisions, tools)
    with monkeypatch.context() as patch:
        patch.setattr(planning, 'format_catalogue', canonical)
        baseline_messages, baseline_tools, baseline_info = await context(ctx, Provider(), decisions, tools)
    assert selected == baseline_tools == tools == before_tools
    assert messages[:1] == baseline_messages[:1]
    assert messages[2:] == baseline_messages[2:]
    assert messages[1]['role'] == baseline_messages[1]['role'] == 'user'
    assert messages[1]['content'] != baseline_messages[1]['content']
    assert json.loads(messages[1]['content']) == json.loads(baseline_messages[1]['content'])
    assert planning.SYSTEM == before_system
    assert info == baseline_info  # Stub counts only; not tokenizer admission evidence.
    assert provider.requests == [(messages, selected)]
    assert list(ctx.db.iterdump()) == before_database and ctx.db.total_changes == before_changes
    evidence = json.loads(messages[1]['content'])
    ids = list(evidence['segments'])
    # Formatting does not narrow legal model selections to a preferred segment.
    for start, end in ((ids[0], ids[0]), (ids[0], ids[-1]), (ids[-1], ids[-1])):
        validate_native_arguments('word.plan_draft', {'source_hash': evidence['source_hash'],
                                  'source_range': {'start': start, 'end': end}}, selected)
    assert ctx.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert ctx.db.execute('SELECT count(*) FROM native_calls').fetchone()[0] == 0
