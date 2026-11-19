"""Host-only stamp transaction/recovery regressions; scripted providers, no model."""
import json

import pytest
from pydantic import ValidationError

from localoffice.chats import current_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.plugins import first_party_registry
from localoffice.recovery import load as recovery
from localoffice.runtime import TaskRunner, WORD_ACTION
from localoffice.storage import canonical, digest
from localoffice.word import creation_repair, creation_submission, requirements
from localoffice.word.contracts import PrepareCreation
from localoffice.word.guidance import build_state
from localoffice.word.page_requirement_projection import project_tools
from localoffice.word.plugin import PageRequirementRejected, WordPlugin
from localoffice.native_tools import action_tools, compact_tools
from localoffice.word.guidance import project_schema
from test_native_runner import NativeProvider
from test_word_content_plan import fixture

OBJECTIVE = 'Use one-inch margins. Draft a brief invitation.'
RAW = {'path': 'report.docx', 'page': {'margin_twips': 1440},
       'blocks': [{'kind': 'paragraph', 'text': 'Please review the document.'}]}


@pytest.fixture
def store(tmp_path):
    value = fixture(tmp_path, OBJECTIVE)
    declare(value)
    yield value
    value.close()


def plan_args():
    return {'source_hash': digest(OBJECTIVE.encode()), 'requirements': [
        {'kind': 'page_geometry', 'source_range': {'start': 's01', 'end': 's01'},
         'field': 'margin', 'equals': 1.0, 'unit': 'in'}]}


def declare(store):
    return requirements.plan(store, 'task', **plan_args())


def stamp(store):
    tools = compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(), ['prepare_creation'])))
    return project_tools(store, 'task', tools)[1]


def preparation_call(store):
    return record_call(store, 'task', {'tool': 'word.prepare_creation', **RAW})


@pytest.mark.asyncio
async def test_registry_forwards_stamp_and_stale_rejection_has_zero_writes(store):
    registry = first_party_registry(store)
    expected = stamp(store)
    call = preparation_call(store)
    declare(store)  # Changed after assembly; the async registry must not lose the stamp.
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    with pytest.raises(PageRequirementRejected) as error:
        await registry.call('word.prepare_creation', task_id='task', native_call_id=call,
                            expected_page_requirement_stamp=expected, **RAW)
    assert error.value.code == 'page_requirements_changed'
    assert list(store.db.iterdump()) == before and store.db.total_changes == changes
    assert creation_repair.get(store, 'task') is None
    assert creation_submission.result(store, 'task', native_call_id=call) is None


def test_authoritative_check_occurs_under_same_transaction_before_capture(store, monkeypatch):
    from localoffice.word import page_requirement_projection as projection
    real_check = projection.require_fresh
    real_capture = creation_repair.capture
    checks = []
    def checked(current_store, task_id, expected):
        assert current_store.db.in_transaction and current_store._transaction_depth >= 1
        checks.append(expected)
        return real_check(current_store, task_id, expected)
    def captured(*args, **kwargs):
        assert len(checks) == 1 and store.db.in_transaction
        return real_capture(*args, **kwargs)
    monkeypatch.setattr(projection, 'require_fresh', checked)
    monkeypatch.setattr(creation_repair, 'capture', captured)
    expected = stamp(store)
    call = preparation_call(store)
    result = WordPlugin(store).prepare_creation(task_id='task', native_call_id=call,
                                                expected_page_requirement_stamp=expected, **RAW)
    assert checks == [expected] and result['status'] == 'prepared'
    assert 'expected_page_requirement_stamp' not in canonical(creation_repair.get(store, 'task')['payload'])
    assert 'expected_page_requirement_stamp' not in store.db.execute(
        'SELECT function FROM native_calls WHERE call_id=?', (call,)).fetchone()[0]


def test_stale_rejection_preserves_existing_proposal_and_ungated_replay(store):
    plugin = WordPlugin(store)
    call = preparation_call(store)
    first = plugin.prepare_creation(task_id='task', native_call_id=call, **RAW)
    record_result(store, 'task', call, first)
    old_stamp = stamp(store)
    declare(store)
    before = list(store.db.iterdump())
    with pytest.raises(PageRequirementRejected):
        plugin.prepare_creation(task_id='task', expected_page_requirement_stamp=old_stamp, **RAW)
    assert list(store.db.iterdump()) == before
    assert plugin.prepare_creation(task_id='task', native_call_id=call, **RAW) == first


def test_explicit_none_stamp_rejects_but_omission_is_ungated(store):
    plugin = WordPlugin(store)
    before = list(store.db.iterdump())
    with pytest.raises(PageRequirementRejected):
        plugin.prepare_creation(task_id='task', expected_page_requirement_stamp=None, **RAW)
    assert list(store.db.iterdump()) == before
    assert plugin.prepare_creation(task_id='task', **RAW)['status'] == 'prepared'


def test_host_stamp_is_not_a_public_tool_argument():
    assert 'expected_page_requirement_stamp' not in canonical(WORD_ACTION.json_schema())
    with pytest.raises(ValidationError):
        PrepareCreation(**RAW, expected_page_requirement_stamp={})


class Scripted(NativeProvider):
    def __init__(self, actions):
        super().__init__(None)
        self.actions = actions

    async def stream(self, messages, *, tools, **kwargs):
        action = self.actions[self.calls]
        self.calls += 1
        if action == 'malformed':
            raw = '<tool_call>broken</tool_call>'
        else:
            raw = '<tool_call>' + json.dumps({'name': action['tool'],
                'arguments': {k: v for k, v in action.items() if k != 'tool'}}) + '</tool_call>'
        yield {'type': 'token', 'text': raw}
        yield {'type': 'finish', 'reason': 'stop'}


def runner_for(store, provider):
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         ('word_tools:task', canonical(build_state(['prepare_creation']))))
    runner = TaskRunner(store, provider, source_planning=True, creation_review=True, requirements_planning=True)
    # A new runner pauses persisted running tasks on startup; emulate start().
    with store.transaction():
        store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
    return runner


@pytest.mark.asyncio
async def test_dispatch_race_is_nonmutation_and_correction_resets_retry_count(store, monkeypatch):
    provider = Scripted([
        {'tool': 'word.prepare_creation', **RAW},
        {'tool': 'word.load_tools', 'operations': ['plan_requirements']},
        {'tool': 'word.plan_requirements', **plan_args()},
        {'tool': 'word.prepare_creation', **RAW},
        'malformed',
        {'tool': 'ask_user', 'text': 'Scripted stop.'},
    ])
    runner = runner_for(store, provider)
    original = runner.registry.handlers['word.prepare_creation']
    observations = []
    async def dispatched(**arguments):
        assert 'expected_page_requirement_stamp' in arguments
        if not observations:
            declare(store)  # Between runtime dispatch and worker retention transaction.
            before = list(store.db.iterdump())
            try:
                return await original(**arguments)
            except PageRequirementRejected:
                assert list(store.db.iterdump()) == before
                observations.append('rejected')
                raise
        state = recovery(store, 'task', current_chat(store))
        assert state['unresolved'] is False and state['mutation'] is False
        observations.append('corrected')
        return await original(**arguments)
    monkeypatch.setitem(runner.registry.handlers, 'word.prepare_creation', dispatched)
    original_load = runner.registry.handlers['word.load_tools']
    async def loaded(**arguments):
        state = recovery(store, 'task', current_chat(store))
        assert state['unresolved'] is True and state['mutation'] is False
        return await original_load(**arguments)
    monkeypatch.setitem(runner.registry.handlers, 'word.load_tools', loaded)
    await runner.run('task', 'background')
    assert observations == ['rejected', 'corrected'], [(e['type'], e.get('code'), e.get('message')) for e in runner.events]
    assert provider.calls == 6 and not [e for e in runner.events if e['type'] == 'error'], runner.events
    assert store.db.execute("SELECT status FROM tasks WHERE id='task'").fetchone()[0] == 'paused'
    assert creation_repair.get(store, 'task') is not None
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_stamp_rejection_does_not_clear_an_older_mutation_failure(store, monkeypatch):
    store.message('tool', canonical({'code': 'prior_mutation', 'message': 'Unresolved'}), 'task',
                  recovery_state={'unresolved': True, 'mutation': True})
    provider = Scripted([{'tool': 'word.prepare_creation', **RAW}, {'tool': 'ask_user', 'text': 'Stop.'}])
    runner = runner_for(store, provider)
    original = runner.registry.handlers['word.prepare_creation']
    async def dispatched(**arguments):
        declare(store)
        return await original(**arguments)
    monkeypatch.setitem(runner.registry.handlers, 'word.prepare_creation', dispatched)
    await runner.run('task', 'background')
    state = recovery(store, 'task', current_chat(store))
    assert state['unresolved'] is True and state['mutation'] is True
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_existing_target_assembly_does_not_install_page_prerequisite(store):
    (store.root / 'report.docx').write_bytes(b'existing target, no document operations in this test')
    runner = runner_for(store, Scripted([]))
    await runner.assemble('task')
    assert runner.page_requirement_stamp is None
    assert 'word.prepare_creation' not in {tool['function']['name'] for tool in runner.native_tools}


@pytest.mark.asyncio
async def test_ungated_native_preparation_dispatch_has_no_host_stamp(store, monkeypatch):
    provider = Scripted([{'tool': 'word.prepare_creation', **RAW}, {'tool': 'ask_user', 'text': 'Stop.'}])
    runner = TaskRunner(store, provider, source_planning=True, creation_review=True)
    with store.transaction():
        store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         ('word_tools:task', canonical(build_state(['prepare_creation']))))
    original = runner.registry.handlers['word.prepare_creation']
    seen = []
    async def dispatched(**arguments):
        assert 'expected_page_requirement_stamp' not in arguments
        seen.append(True)
        return await original(**arguments)
    monkeypatch.setitem(runner.registry.handlers, 'word.prepare_creation', dispatched)
    await runner.run('task', 'background')
    assert seen == [True] and provider.calls == 2
    assert not [e for e in runner.events if e['type'] in ('error', 'tool_error')], runner.events
