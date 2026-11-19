"""Assembly-only pressure tests; completed_changes is the authoritative boundary stub."""
import copy
import json
from types import SimpleNamespace

import pytest

from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.native_tools import validate_history
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical, digest
from localoffice.word.guidance import build_state, fingerprint, load, loaded_receipt, selected
from test_creation_proposal_context_recycling import exact
from test_word_content_plan import fixture


READS = {'word.list_objects', 'word.find_text', 'word.read_objects'}
OBJECTIVE = 'Keep the full invitation, Δ and 😀. Review the document before finishing.'
REVISION = 'a' * 64
CREATION = ['prepare_creation', 'edit_creation_styles', 'finalize_creation']


class Pressure:
    native_tool_protocol = True
    context_limit = 4096

    def __init__(self, pressured=True):
        self.pressured = pressured
        self.counts = []

    def status(self):
        return {'loaded': True, 'constrained_decoding': True}

    async def count_tokens(self, messages, *, tools):
        validate_history(messages)
        names = {tool['function']['name'] for tool in tools}
        # Partial removal must not accidentally satisfy this counting stub.
        value = 3500 if self.pressured and names & READS else 3000
        self.counts.append((names, value))
        return value

    async def stream(self, *args, **kwargs):
        pytest.fail('Assembly tests must never invoke inference.')
        yield


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = fixture(tmp_path, OBJECTIVE)
    store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',
                     ('report.docx', 'completed-creation', None, REVISION))
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                     ('word_tools:task', canonical(build_state(CREATION))))
    chat = store.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?', ('task',)).fetchone()[0]
    evidence = {'interpretation': 'Executed changes, not completion proof.',
                'receipts': [{'receipt_id': 'completed-creation', 'status': 'validated',
                              'candidate_revision': REVISION, 'matches_current_revision': True,
                              'tool': 'word.create', 'operation_count': 0, 'changes': [],
                              'details_omitted': True}], 'older_receipts_omitted': 0}
    ctx = SimpleNamespace(store=store, evidence=evidence, runners=[])

    def completed(actual_store, task_id, chat_id, path, revision):
        assert actual_store is store
        assert (task_id, chat_id, path, revision) == ('task', chat, 'report.docx', REVISION)
        return copy.deepcopy(ctx.evidence)

    def forbidden(*args, **kwargs):
        pytest.fail('Context assembly must not dispatch document workers.')

    monkeypatch.setattr('localoffice.word.progress.completed_changes', completed)
    monkeypatch.setattr('localoffice.word.plugin.WordPlugin._execute', forbidden)
    call = record_call(store, 'task', {'tool': 'word.finalize_creation', 'ref': 'cr_' + '1' * 32})
    record_result(store, 'task', call, {'status': 'validated', 'receipt_id': 'completed-creation',
                                     'candidate_revision': REVISION, 'note': 'Latest exact result Δ 😀'})

    def runner(pressured=True, **options):
        result = TaskRunner(store, Pressure(pressured), source_planning=True, creation_review=True,
                            **options)
        ctx.runners.append(result)
        return result

    ctx.runner = runner
    ctx.call = call
    immutable_tables = ('operations', 'candidates', 'native_calls', 'messages')
    before = {name: [tuple(row) for row in store.db.execute('SELECT * FROM ' + name)]
              for name in immutable_tables}
    yield ctx
    for name, rows in before.items():
        assert [tuple(row) for row in store.db.execute('SELECT * FROM ' + name)] == rows
    assert not (tmp_path / 'report.docx').exists()
    store.close()


def tool_names(runner):
    return {tool['function']['name'] for tool in runner.native_tools}


def saved(store):
    return store.db.execute("SELECT value FROM meta WHERE key='word_tools:task'").fetchone()[0]


def recycle_count(store):
    return store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]


@pytest.mark.asyncio
async def test_filtered_empty_creation_selection_recycles_reads_preserving_exact_context(context):
    runner = context.runner(False)
    baseline = await runner.assemble('task')
    assert runner.word_tool_state['operations'] == []
    assert selected(context.store, 'task')['operations'] == CREATION
    assert READS <= tool_names(runner)
    evidence_before = copy.deepcopy(context.evidence)

    runner.provider.pressured = True
    messages = await runner.assemble('task')
    assert exact(messages) == exact(baseline)
    assert any(m['role'] == 'user' and m['content'] == OBJECTIVE for m in messages)
    assert messages[-2:] == baseline[-2:]
    assert messages[-2]['tool_calls'][0]['id'] == context.call
    assert 'Latest exact result Δ 😀' in messages[-1]['content']
    assert exact(messages)['target']['expected_revision'] == REVISION
    assert exact(messages)['completed_word_changes']['receipts'][0]['matches_current_revision'] is True
    assert context.evidence == evidence_before
    assert runner.word_tool_state['operations'] == []
    assert runner.word_tool_state['inspection_unloaded'] is True
    assert selected(context.store, 'task') == runner.word_tool_state
    assert not READS & tool_names(runner)
    assert {'word.load_tools', 'ask_user'} <= tool_names(runner)
    assert runner.context_info['input_tokens'] == 3000
    assert runner.context_info['evicted_read_tools'] == sorted(READS)
    assert runner.context_info['evicted_word_operations'] == []
    assert any(READS <= names and count == 3500 for names, count in runner.provider.counts)
    assert recycle_count(context.store) == 1
    metric = json.loads(context.store.db.execute(
        "SELECT body FROM task_measurements WHERE kind='context_recycle'").fetchone()[0])
    assert metric['reason'] == 'validated_artifact'
    # A fresh runner honors the persisted selection without another recycle.
    restarted = context.runner()
    await restarted.assemble('task')
    assert not READS & tool_names(restarted)
    assert recycle_count(context.store) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('isolated', [False, True])
async def test_existing_target_empty_ops_never_auto_exposes_draft_or_catalogue(context, monkeypatch, isolated):
    def forbidden_catalogue(*args, **kwargs):
        pytest.fail('Existing-target empty operations must not assemble a drafting catalogue.')

    monkeypatch.setattr('localoffice.word.objective_evidence.catalogue', forbidden_catalogue)
    runner = context.runner(requirements_planning=True, focused_drafting=True,
                            isolated_draft_planning=isolated)
    messages = await runner.assemble('task')
    assert runner.word_tool_state['operations'] == []
    assert runner.word_tool_state['inspection_unloaded'] is True
    assert 'word.plan_draft' not in tool_names(runner)
    assert 'Exact objective evidence catalogue:' not in canonical(messages)
    loader = next(t['function'] for t in runner.native_tools if t['function']['name'] == 'word.load_tools')
    assert 'plan_draft' in loader['parameters']['properties']['operations']['items']['enum']
    assert any(m['role'] == 'user' and m['content'] == OBJECTIVE for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize('isolated', [False, True])
async def test_existing_target_explicit_draft_load_still_exposes_exact_catalogue(context, isolated):
    runner = context.runner(False, requirements_planning=True, focused_drafting=True,
                            isolated_draft_planning=isolated)
    context.store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
    load(context.store, 'task', ['plan_draft'])
    messages = await runner.assemble('task')
    assert runner.word_tool_state['operations'] == ['plan_draft']
    tools = [t['function'] for t in runner.native_tools if t['function']['name'] == 'word.plan_draft']
    assert len(tools) == 1
    assert tools[0]['parameters']['properties']['source_hash'] == {'const': digest(OBJECTIVE.encode())}
    assert 'Exact objective evidence catalogue:' in messages[0]['content']
    assert 'word.submit_draft' not in tool_names(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize('isolated', [False, True])
async def test_new_target_empty_ops_still_auto_exposes_draft(tmp_path, monkeypatch, isolated):
    from localoffice.word.requirements import plan

    def forbidden_worker(*args, **kwargs):
        pytest.fail('Draft catalogue assembly must not dispatch workers.')

    monkeypatch.setattr('localoffice.word.plugin.WordPlugin._execute', forbidden_worker)
    store = fixture(tmp_path, OBJECTIVE)
    try:
        plan(store, 'task', source_hash=digest(OBJECTIVE.encode()), requirements=[])
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('word_tools:task', canonical(
            {'operations': [], 'source_hash': fingerprint([]), 'guidance': {}})))
        runner = TaskRunner(store, Pressure(False), source_planning=True, creation_review=True,
                            requirements_planning=True, focused_drafting=True,
                            isolated_draft_planning=isolated)
        messages = await runner.assemble('task')
        assert 'word.plan_draft' in tool_names(runner)
        tool = next(t['function'] for t in runner.native_tools if t['function']['name'] == 'word.plan_draft')
        assert tool['parameters']['properties']['source_hash'] == {'const': digest(OBJECTIVE.encode())}
        assert digest(OBJECTIVE.encode()) in canonical(messages)
        assert {'word.load_tools', 'ask_user'} <= tool_names(runner)
        assert 'word.submit_draft' not in tool_names(runner)
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM native_calls').fetchone()[0] == 0
        assert not store.candidate('report.docx')
        assert not (tmp_path / 'report.docx').exists()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_explicit_inspect_load_restores_all_three_tools(context):
    runner = context.runner()
    await runner.assemble('task')
    prior = saved(context.store)
    inspect = build_state(['inspect'])
    # If the explicit load does not fit, preflight must reject it, not hide it.
    with pytest.raises(HarnessError) as error:
        await runner.assemble('task', word_tool_override=inspect,
                              pending_tool_result=loaded_receipt(inspect))
    assert error.value.code == 'context_capacity'
    assert saved(context.store) == prior
    assert READS <= tool_names(runner)
    runner.provider.pressured = False
    await runner.assemble('task', word_tool_override=inspect,
                          pending_tool_result=loaded_receipt(inspect))
    context.store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
    load(context.store, 'task', ['inspect'])
    await runner.assemble('task')
    assert READS <= tool_names(runner)
    assert runner.word_tool_state['operations'] == ['inspect']
    assert not runner.word_tool_state.get('inspection_unloaded', False)
    assert recycle_count(context.store) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', ['none', 'historical', 'unrelated'])
async def test_without_current_owned_completion_pressure_cannot_restrict_reads(context, receipt):
    if receipt == 'none':
        context.evidence = None
    elif receipt == 'historical':
        context.evidence['receipts'][0].update(candidate_revision='b' * 64, matches_current_revision=False)
    else:
        # The authoritative scoped projection excludes another task's receipt.
        context.evidence = {'receipts': [], 'older_receipts_omitted': 0}
    before = saved(context.store)
    runner = context.runner()
    with pytest.raises(HarnessError) as error:
        await runner.assemble('task')
    assert error.value.code == 'context_capacity'
    assert READS <= tool_names(runner)
    assert saved(context.store) == before
    assert recycle_count(context.store) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['pending_result', 'override'])
async def test_pending_admission_cannot_implicitly_recycle(context, guard):
    before = saved(context.store)
    runner = context.runner()
    kwargs = ({'pending_tool_result': {'loaded_operations': []}} if guard == 'pending_result'
              else {'word_tool_override': build_state(CREATION)})
    with pytest.raises(HarnessError) as error:
        await runner.assemble('task', **kwargs)
    assert error.value.code == 'context_capacity'
    assert READS <= tool_names(runner)
    assert saved(context.store) == before
    assert recycle_count(context.store) == 0


@pytest.mark.asyncio
async def test_persisted_unloaded_flag_is_ignored_when_completion_becomes_stale(context):
    await context.runner().assemble('task')
    before = saved(context.store)
    assert json.loads(before)['inspection_unloaded'] is True
    context.evidence['receipts'][0].update(candidate_revision='b' * 64, matches_current_revision=False)
    runner = context.runner(False)
    await runner.assemble('task')
    assert READS <= tool_names(runner)
    runner.provider.pressured = True
    with pytest.raises(HarnessError) as error:
        await runner.assemble('task')
    assert error.value.code == 'context_capacity'
    assert READS <= tool_names(runner)
    assert saved(context.store) == before
    assert recycle_count(context.store) == 1
