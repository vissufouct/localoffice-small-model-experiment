"""Durable opt-in inference reservations; no inference or runtime routing here."""
import json

from .errors import HarnessError
from .native_history import _owner
from .storage import canonical


PREFIX = 'task_draft_budget:'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate budget field')
        result[key] = value
    return result


def _read(store, task_id):
    _owner(store, task_id)
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + task_id,)).fetchone()
    if row is None:
        if store.db.execute(
                "SELECT 1 FROM task_measurements WHERE task_id=? AND kind='inference' LIMIT 1",
                (task_id,)).fetchone():
            raise HarnessError('draft_budget_migration',
                               'Prior inference has no durable draft budget. Start a new opt-in task.')
        return None
    try:
        state = json.loads(row[0], object_pairs_hook=_unique_object)
        if (not isinstance(state, dict) or set(state) != {'consumed', 'max_steps'}
                or type(state['consumed']) is not int or type(state['max_steps']) is not int
                or state['max_steps'] < 1 or not 0 <= state['consumed'] <= state['max_steps']):
            raise ValueError('Invalid budget state')
    except (ValueError, TypeError, RecursionError) as exc:
        raise HarnessError('draft_budget_corrupt', 'Stored draft budget is invalid; no reset is allowed.') from exc
    return state


def get(store, task_id):
    """Return consumed steps, or None before opt-in initialization; never write."""
    with store.lock:
        state = _read(store, task_id)
        return None if state is None else state['consumed']


def reserve(store, task_id, max_steps):
    """Commit one step BEFORE inference; interruptions do not refund it.

    Call outside any enclosing transaction and before recording the inference
    measurement. The caller alone opts a task into this budget.
    """
    with store.lock:
        if store.db.in_transaction or getattr(store, '_transaction_depth', 0):
            raise HarnessError('draft_budget_transaction',
                               'Reserve outside an enclosing transaction so the step commits before inference.')
        return _reserve_transaction(store, task_id, max_steps)


def _reserve_transaction(store, task_id, max_steps):
    with store.transaction():
        state = _read(store, task_id)
        if type(max_steps) is not int or max_steps < 1:
            raise HarnessError('draft_budget_limit', 'max_steps must be a positive integer.')
        if state is None:
            state = {'consumed': 0, 'max_steps': max_steps}
        elif state['max_steps'] != max_steps:
            raise HarnessError('draft_budget_limit_change', 'Resume with the original draft step limit.')
        if state['consumed'] >= max_steps:
            raise HarnessError('step_limit', 'Task reached its durable inference step limit.')
        state['consumed'] += 1
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         (PREFIX + task_id, canonical(state)))
        return state['consumed']
