"""Exact current generated-draft pair projection; no history or document writes."""
import json

from ..errors import HarnessError
from ..native_history import _owner
from ..storage import canonical, digest
from . import draft_fragments

STATE_KEY = 'generated_draft'
_RECEIPT_PREFIX = 'TOOL RECEIPT / SOURCE EVIDENCE: '


def project_pair(store, task_id, assistant, tool, generated_snapshot):
    """Return a labelled assistant history summary, or None to retain the pair.

    Call after native_history.project_message, before pruning. The caller must
    retain the exact generated_snapshot under STATE_KEY in authoritative state
    for as long as this summary remains visible. No semantic/placement proof is
    inferred. Missing, stale or inconsistent evidence never permits collapse.
    """
    with store.lock:
        try:
            return _project(store, task_id, assistant, tool, generated_snapshot)
        except (HarnessError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            return None


def _project(store, task_id, assistant, tool, generated_snapshot):
    chat_id = _owner(store, task_id)
    state = draft_fragments.get(store, task_id)
    if state is None or state['status'] != 'drafted':
        return None
    task = store.db.execute('SELECT objective,artifact FROM tasks WHERE id=?', (task_id,)).fetchone()
    if (task is None or state['source_text'] != task['objective']
            or state['request']['source_hash'] != digest(task['objective'].encode('utf-8'))
            or state['owner'] != {'task_id': task_id, 'chat_id': chat_id, 'target': task['artifact']}):
        return None
    expected_snapshot = {'ref': state['ref'], 'text': state['text'], 'review': 'unassessed'}
    if canonical(generated_snapshot) != canonical(expected_snapshot):
        return None
    if not isinstance(assistant, dict) or not isinstance(assistant.get('tool_calls'), list):
        return None
    if len(assistant['tool_calls']) != 1:
        return None
    call_id = assistant['tool_calls'][0]['id']
    if not isinstance(call_id, str):
        return None
    row = store.db.execute('''SELECT n.*,a.content AS action,t.content AS receipt
        FROM native_calls n
        JOIN messages a ON a.id=n.assistant_message AND a.task_id=n.task_id AND a.role='assistant'
        JOIN message_chats ac ON ac.message_id=a.id AND ac.chat_id=n.chat_id
        JOIN messages t ON t.id=n.result_message AND t.task_id=n.task_id AND t.role='tool'
        JOIN message_chats tc ON tc.message_id=t.id AND tc.chat_id=n.chat_id
        WHERE n.call_id=? AND n.task_id=? AND n.chat_id=?''', (call_id, task_id, chat_id)).fetchone()
    if row is None or row['result_message'] <= row['assistant_message']:
        return None
    function = json.loads(row['function'])
    expected_request = {'tool': 'word.submit_draft', 'ref': state['ref'], 'text': state['text']}
    expected_function = {'name': 'word.submit_draft',
                         'arguments': {'ref': state['ref'], 'text': state['text']}}
    if canonical(function) != canonical(expected_function) or row['action'] != canonical(expected_request):
        return None
    expected_assistant = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': expected_function}]}
    expected_tool = {'role': 'tool', 'tool_call_id': call_id, 'content': _RECEIPT_PREFIX + row['receipt']}
    if canonical(assistant) != canonical(expected_assistant) or canonical(tool) != canonical(expected_tool):
        return None
    saved = draft_fragments.result(store, task_id, function['arguments'], native_call_id=call_id)
    if (saved is None or canonical(saved) != row['receipt']
            or saved.get('ref') != state['ref'] or saved.get('generation') != state['generation']
            or saved.get('source_hash') != state['request']['source_hash']
            or saved.get('status') != 'drafted' or saved.get('document_changed') is not False
            or saved.get('semantic_validation') != 'unassessed'):
        return None
    return {'role': 'assistant', 'content':
            f"Historical draft submission: exact text/reference in {STATE_KEY}; placement and meaning unverified."}
