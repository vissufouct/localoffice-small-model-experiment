from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from localoffice import draft_budget, metrics
from localoffice.chats import create_chat, current_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore


@pytest.fixture
def store(tmp_path):
    value = ProjectStore(tmp_path)
    with value.transaction():
        value.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('task', 'Draft', 'running', 'live', 'report.docx', 1, 1))
        value.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', current_chat(value)))
    yield value
    value.close()


def unchanged_failure(store, operation, code):
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    with pytest.raises(HarnessError) as error:
        operation()
    assert error.value.code == code
    assert list(store.db.iterdump()) == before
    assert store.db.total_changes == changes


def test_read_only_initial_state_and_limit(store):
    before = list(store.db.iterdump())
    assert draft_budget.get(store, 'task') is None
    assert list(store.db.iterdump()) == before
    assert [draft_budget.reserve(store, 'task', 2) for _ in range(2)] == [1, 2]
    assert draft_budget.get(store, 'task') == 2
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 2), 'step_limit')


def test_interrupt_and_restart_consume_reservation(store, tmp_path):
    assert draft_budget.reserve(store, 'task', 3) == 1
    # No inference/measurement follows: a process interruption still consumes it.
    store.close()
    reopened = ProjectStore(tmp_path)
    try:
        assert draft_budget.get(reopened, 'task') == 1
        assert draft_budget.reserve(reopened, 'task', 3) == 2
        unchanged_failure(reopened, lambda: draft_budget.reserve(reopened, 'task', 4),
                          'draft_budget_limit_change')
    finally:
        reopened.close()


@pytest.mark.parametrize('limit', [0, -1, True, 1.0, '10', None])
def test_invalid_limit_does_not_initialize(store, limit):
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', limit), 'draft_budget_limit')


@pytest.mark.parametrize('raw', [
    'broken', 'null', '[]', '{}',
    '{"consumed":true,"max_steps":10}', '{"consumed":1.0,"max_steps":10}',
    '{"consumed":-1,"max_steps":10}', '{"consumed":11,"max_steps":10}',
    '{"consumed":0,"max_steps":0}', '{"consumed":0,"max_steps":true}',
    '{"consumed":0,"max_steps":"10"}', '{"consumed":0,"max_steps":10,"extra":1}',
    '{"consumed":1,"consumed":0,"max_steps":10}',
])
def test_corruption_never_resets_or_writes(store, raw):
    with store.transaction():
        store.db.execute('INSERT INTO meta VALUES(?,?)', (draft_budget.PREFIX + 'task', raw))
    unchanged_failure(store, lambda: draft_budget.get(store, 'task'), 'draft_budget_corrupt')
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 10), 'draft_budget_corrupt')


def test_scope_before_initialization_and_after_reservation(store):
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'missing', 10), 'chat_scope')
    draft_budget.reserve(store, 'task', 10)
    select_chat(store, create_chat(store))
    unchanged_failure(store, lambda: draft_budget.get(store, 'task'), 'chat_scope')
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 10), 'chat_scope')


@pytest.mark.parametrize('status', ['running', 'completed', 'failed', 'cancelled'])
def test_prior_inference_without_counter_rejects_migration(store, status):
    measurement = metrics.begin(store, 'task', 'inference')
    metrics.finish(store, measurement, status=status)
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 10), 'draft_budget_migration')
    unchanged_failure(store, lambda: draft_budget.get(store, 'task'), 'draft_budget_migration')


def test_non_inference_and_other_task_measurements_do_not_count(store):
    metrics.begin(store, 'task', 'tool')
    metrics.begin(store, 'other', 'inference')
    assert draft_budget.reserve(store, 'task', 2) == 1
    metrics.begin(store, 'task', 'inference')
    assert draft_budget.reserve(store, 'task', 2) == 2


def test_failed_database_write_is_atomic(store):
    draft_budget.reserve(store, 'task', 2)
    with store.transaction():
        store.db.execute("""CREATE TEMP TRIGGER reject_budget BEFORE INSERT ON meta
            WHEN NEW.key='task_draft_budget:task'
            BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    with pytest.raises(sqlite3.IntegrityError, match='injected write failure'):
        draft_budget.reserve(store, 'task', 2)
    assert list(store.db.iterdump()) == before
    assert store.db.total_changes == changes
    assert draft_budget.get(store, 'task') == 1


def test_parallel_reservations_are_serialized(store):
    def attempt(_):
        try:
            return draft_budget.reserve(store, 'task', 10)
        except HarnessError as exc:
            assert exc.code == 'step_limit'
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(15)))
    assert sorted(value for value in results if value is not None) == list(range(1, 11))
    assert results.count(None) == 5
    assert draft_budget.get(store, 'task') == 10


def test_enclosing_transaction_cannot_make_reservation_rollbackable(store):
    with store.transaction():
        unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 10),
                          'draft_budget_transaction')
    assert draft_budget.get(store, 'task') is None
    assert draft_budget.reserve(store, 'task', 10) == 1


def test_changed_limit_cannot_reset_exhausted_budget(store):
    assert draft_budget.reserve(store, 'task', 1) == 1
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 2),
                      'draft_budget_limit_change')
    unchanged_failure(store, lambda: draft_budget.reserve(store, 'task', 1), 'step_limit')
