"""Read-only permission to resume document capabilities after draft submission.

The caller owns target absence, requirements readiness and current tool selection.
This helper neither chooses tools nor places, accepts or executes draft content.
"""
import json

from ..errors import HarnessError
from ..storage import canonical
from . import draft_fragments
from .content_plan import source
from .contracts import LoadTools
from .guidance import build_state, loaded_receipt

MAX_CALLS = 128


def _json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError) as exc:
        raise HarnessError('state_corrupt', 'Draft routing history is malformed.') from exc


def _pair(store, call, function, owner):
    """Authenticate the completed native pair, including message ownership."""
    if call['chat_id'] != owner['chat_id']:
        raise HarnessError('chat_scope', 'Draft routing call belongs to another chat.')
    messages = []
    for identity, role in ((call['assistant_message'], 'assistant'), (call['result_message'], 'tool')):
        row = store.db.execute('''SELECT m.*,c.chat_id FROM messages m
            LEFT JOIN message_chats c ON c.message_id=m.id WHERE m.id=?''', (identity,)).fetchone()
        if (row is None or row['task_id'] != owner['task_id'] or row['chat_id'] != owner['chat_id']
                or row['role'] != role):
            raise HarnessError('chat_scope', 'Draft routing message ownership is invalid.')
        messages.append(row)
    if (messages[1]['id'] <= messages[0]['id']
            or messages[0]['content'] != canonical({'tool': function['name'], **function['arguments']})):
        raise HarnessError('state_corrupt', 'Draft routing native pair changed.')
    return _json(messages[1]['content'])


def should_return_to_document(store, task_id):
    """True only for fresh, completed native drafting without a later tool choice.

    Recovered results qualify after reconciliation. Pending calls, direct API
    submissions and argument equality alone do not establish completion. The
    earliest exact submission anchors ordering, so an identical retry cannot
    erase a later explicit load. Corrupt/scope-invalid evidence fails closed.
    """
    with store.transaction():
        state = draft_fragments.get(store, task_id)
        if state is None or state['status'] != 'drafted':
            return False
        _, text, revision = source(store, task_id)
        if text != state['source_text'] or revision != state['request']['source_hash']:
            return False
        if store.db.execute('SELECT 1 FROM native_calls WHERE task_id=? AND result_message IS NULL LIMIT 1',
                            (task_id,)).fetchone():
            return False
        calls = store.db.execute('SELECT * FROM native_calls WHERE task_id=? ORDER BY assistant_message LIMIT ?',
                                 (task_id, MAX_CALLS + 1)).fetchall()
        if len(calls) > MAX_CALLS:
            raise HarnessError('draft_routing_limit', 'Draft routing history exceeds its bounded read limit.')
        anchor = None
        newer_loads = []
        for call in calls:
            function = _json(call['function'])
            if (not isinstance(function, dict) or set(function) != {'name', 'arguments'}
                    or not isinstance(function['name'], str) or not isinstance(function['arguments'], dict)
                    or 'tool' in function['arguments']):
                raise HarnessError('state_corrupt', 'Draft routing call shape changed.')
            if function['name'] not in ('word.submit_draft', 'word.load_tools'):
                continue
            response = _pair(store, call, function, state['owner'])
            if function['name'] == 'word.submit_draft':
                receipt = draft_fragments.result(store, task_id, {'tool': function['name'], **function['arguments']},
                                                  native_call_id=call['call_id'])
                if receipt is None:
                    continue
                if canonical(response) != canonical(receipt):
                    raise HarnessError('state_corrupt', 'Draft routing result differs from its immutable receipt.')
                if (receipt['status'] == 'drafted' and receipt['ref'] == state['ref']
                        and receipt['generation'] == state['generation']
                        and function['arguments'].get('text') == state['text'] and anchor is None):
                    anchor = call['assistant_message']
            else:
                if isinstance(response, dict) and 'code' in response:
                    continue  # A rejected load did not select capabilities.
                try:
                    request = LoadTools.model_validate({'tool': function['name'], **function['arguments']})
                    expected = loaded_receipt(build_state(request.operations))
                except (ValueError, TypeError) as exc:
                    raise HarnessError('state_corrupt', 'Draft routing load arguments changed.') from exc
                if canonical(response) != canonical(expected):
                    raise HarnessError('state_corrupt', 'Draft routing load result differs from its request.')
                newer_loads.append(call['assistant_message'])
        return anchor is not None and not any(identity > anchor for identity in newer_loads)
