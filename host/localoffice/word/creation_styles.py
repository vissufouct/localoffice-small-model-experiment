"""Explicit named-style proposal edits with atomic revision and native receipts."""
import copy
import json
import re

from ..errors import HarnessError
from ..storage import canonical, digest
from .contracts import EditCreationStyles
from . import creation_repair as repair
from .creation_revision import revise, replay_receipt

PREFIX = 'word_creation_style_result:'


def _hash(value):
    return digest(canonical(value).encode())


def normalize(arguments):
    """Canonical typed request; absent typography stays absent, false stays false."""
    request = EditCreationStyles.model_validate(arguments)
    return {'tool': request.tool, 'ref': request.ref,
            'changes': [change.model_dump(exclude_none=True) for change in request.changes]}


def _owner(store, task_id):
    owner = repair._owner(store, task_id)
    return {'task_id': task_id, 'chat_id': owner['chat_id'], 'artifact': owner['artifact']}


def _native_call(store, task_id, call_id, request):
    owner = _owner(store, task_id)
    if not isinstance(call_id, str) or not re.fullmatch(r'call_[0-9a-f]{32}', call_id):
        raise HarnessError('native_history', 'Expected a host-issued native call identity.')
    row = store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                           (call_id, task_id, owner['chat_id'])).fetchone()
    if row is None:
        raise HarnessError('native_history', 'Style edit call belongs to another task or chat.')
    try:
        function = json.loads(row['function'])
        if function['name'] != 'word.edit_creation_styles' or normalize(function['arguments']) != request:
            raise ValueError('request')
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('native_history', 'Style edit call has different or invalid arguments.') from exc
    return owner, row


def _receipt(store, owner, call_id, request):
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + call_id,)).fetchone()
    if row is None:
        return None
    try:
        envelope = json.loads(row[0]); state = envelope['state']
        if envelope['hash'] != _hash(state): raise ValueError('envelope')
        if state['version'] != 1 or state['call_id'] != call_id: raise ValueError('identity')
        if state['request_hash'] != _hash(state['request']): raise ValueError('request')
        if state['receipt_hash'] != _hash(state['receipt']): raise ValueError('receipt')
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('state_corrupt', 'Style edit receipt failed its integrity check.') from exc
    if state['owner'] != owner or state['request'] != request:
        raise HarnessError('native_history', 'Style edit receipt has different ownership or arguments.')
    return state['receipt']


def result(store, task_id, args, *, native_call_id):
    """Exact historical execution evidence only; never activates or edits state."""
    request = normalize(args)
    with store.transaction():
        owner, _ = _native_call(store, task_id, native_call_id, request)
        return _receipt(store, owner, native_call_id, request)


def edit(store, task_id, *, ref, changes, native_call_id=None):
    request = normalize({'ref': ref, 'changes': changes})
    origin = {'ref': ref, 'request_hash': _hash(request)}
    with store.transaction():
        owner = _owner(store, task_id)
        if native_call_id is not None:
            owner, row = _native_call(store, task_id, native_call_id, request)
            receipt = _receipt(store, owner, native_call_id, request)
            if receipt is not None: return receipt
            if row['result_message'] is not None:
                raise HarnessError('native_history', 'Completed style edit call has no recorded receipt.')
        state = repair.get(store, task_id)
        if state is None:
            raise HarnessError('creation_repair_reference', 'Unknown creation proposal.')
        repair._fresh(store, state)
        if (state.get('style_revision_origin') == origin
                and state.get('style_revision_result_ref') == state['ref']):
            receipt = replay_receipt(store, state)
        else:
            state = repair.get(store, task_id, ref)
            explicit = state['payload'].get('styles', [])
            positions = {style['id']: index for index, style in enumerate(explicit)}
            if len(positions) != len(explicit):
                raise HarnessError('creation_revision', 'Explicit style IDs are ambiguous in this proposal.')
            declarations = [change for change in request['changes'] if change['op'] == 'declare']
            if len(explicit) + len(declarations) > 16:
                raise HarnessError('creation_revision', 'The proposal permits at most 16 explicit style declarations.')
            patches = []
            if 'styles' not in state['payload']:
                patches.append({'op': 'set', 'path': 'styles', 'value': []})
            next_index = len(explicit)
            for change in request['changes']:
                style_id = change['id']
                typography = {key: value for key, value in change.items() if key not in ('op', 'id')}
                if change['op'] == 'declare':
                    if style_id in positions:
                        raise HarnessError('creation_revision', 'Style ID is already explicitly declared; use update.')
                    patches.append({'op': 'insert', 'path': f'styles.{next_index}',
                                    'value': {'id': style_id, **typography}})
                    next_index += 1
                else:
                    if style_id not in positions:
                        raise HarnessError('creation_revision',
                            'Update requires an explicit style ID; use declare with font and size_points. Implicit Normal has no declaration.')
                    index = positions[style_id]
                    updated = copy.deepcopy(explicit[index])
                    if 'size_points' in typography:
                        updated.pop('size_half_points', None)
                    updated.update(typography)
                    patches.append({'op': 'set', 'path': f'styles.{index}', 'value': updated})
            receipt = revise(store, task_id, ref, patches)
            revised = repair.get(store, task_id, receipt['ref'])
            revised['style_revision_origin'] = origin
            revised['style_revision_result_ref'] = revised['ref']
            repair._save(store, revised)
        if native_call_id is not None:
            saved = {'version': 1, 'owner': owner, 'call_id': native_call_id,
                     'request': request, 'request_hash': _hash(request),
                     'receipt': receipt, 'receipt_hash': _hash(receipt)}
            store.db.execute('INSERT INTO meta VALUES(?,?)',
                             (PREFIX + native_call_id, canonical({'state': saved, 'hash': _hash(saved)})))
        return receipt
