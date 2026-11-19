"""Complete current selected-plan facts and exact native-pair prompt projection.

No routing, activation, source interpretation or storage mutation. Historical
arguments may be projected ONLY when all their selected checks are present in
verified current state. Full submitted arguments/receipts remain in SQLite.
"""
import json

from ..chats import current_chat
from ..storage import canonical
from .content_plan import source
from .requirements import get, normalize, result

MAX_BYTES = 2048
STATE_KEY = 'selected_requirement_plan'
_RECEIPT_PREFIX = 'TOOL RECEIPT / SOURCE EVIDENCE: '


def _snapshot(state):
    spans = []
    entries = []
    for entry, span in zip(state['request']['requirements'], state['source_spans']):
        pair = [span['start'], span['end']]
        if pair not in spans:
            spans.append(pair)
        entries.append({**{key: value for key, value in entry.items()
                          if key not in ('source_quote', 'source_range')},
                        'source_span': spans.index(pair)})
    return {'generation': state['generation'], 'source_hash': state['request']['source_hash'],
            'coverage': 'unassessed', 'interpretation': 'model_selected', 'complete': True,
            'source_spans': spans, 'requirements': entries}


def snapshot(store, task_id, *, max_bytes=MAX_BYTES):
    """All selected checks, with deduplicated code-point [start,end) source spans.

    None means absent, stale or unable to fit completely; never treat it as an
    empty plan. Source text is the exact current objective, not a new quotation.
    Ownership/integrity failures propagate. No field/value/target is inferred.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError('Plan projection budget must be between 1 and 2048 bytes.')
    with store.lock:
        state = get(store, task_id)
        if state is None:
            return None
        if source(store, task_id)[2] != state['request']['source_hash']:
            return None
        projected = _snapshot(state)
        if len(canonical(projected).encode('utf-8')) > max_bytes:
            return None
        return projected


def project_pair(store, task_id, call_id, assistant, tool, visible_plan):
    """Return one marked historical assistant summary, or None to keep the pair.

    Call after native_history.project_message. The caller must include the exact
    visible_plan under STATE_KEY in authoritative prompt state and retain it
    whenever this pair is retained. This function does not edit its inputs.
    """
    with store.lock:
        current = snapshot(store, task_id)
        if current is None or canonical(visible_plan) != canonical(current):
            return None
        row = store.db.execute('''SELECT n.*,m.content AS receipt FROM native_calls n
            JOIN messages m ON m.id=n.result_message
            WHERE n.call_id=? AND n.task_id=? AND n.chat_id=?''',
            (call_id, task_id, current_chat(store))).fetchone()
        if row is None:
            return None
        function = json.loads(row['function'])
        if function.get('name') != 'word.plan_requirements':
            return None
        expected_assistant = {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': call_id, 'type': 'function', 'function': function}]}
        expected_tool = {'role': 'tool', 'tool_call_id': call_id,
                         'content': _RECEIPT_PREFIX + row['receipt']}
        if canonical(assistant) != canonical(expected_assistant) or canonical(tool) != canonical(expected_tool):
            return None
        state = get(store, task_id)
        if normalize(function['arguments']) != state['request']:
            return None
        saved = result(store, task_id, function['arguments'], native_call_id=call_id)
        if (saved is None or canonical(saved) != row['receipt']
                or saved.get('generation') != current['generation']
                or saved.get('source_hash') != current['source_hash']
                or saved.get('count') != len(current['requirements'])
                or saved.get('status') not in ('planned', 'unassessed')
                or saved.get('document_changed') is not False):
            return None
        return {'role': 'assistant', 'content': canonical({'historical_tool_summary': {
            'tool': 'word.plan_requirements', 'call_id': call_id,
            'generation': saved['generation'], 'status': saved['status'],
            'document_changed': False, 'details_in_current_state': STATE_KEY}})}
