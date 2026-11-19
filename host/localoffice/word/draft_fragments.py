"""Source-scoped generated drafts. Metadata only; no semantic certification.

History stores immutable pending and drafted snapshots separately. Only an exact
native call receipt proves prior execution; matching arguments never do.
"""
import json
import re
import uuid
from typing import Literal

from pydantic import Field, field_validator

from ..errors import HarnessError
from ..storage import canonical, digest
from .content_plan import _plan_owner, source
from .contracts import Contract, Hash
from .objective_evidence import VERSION, resolve_text
from .requirements import SourceRange

PREFIX = 'draft_fragment:'
HISTORY_PREFIX = 'draft_fragment_history:'
RESULT_PREFIX = 'draft_fragment_result:'


class PlanDraft(Contract):
    tool: Literal['word.plan_draft'] = 'word.plan_draft'
    source_hash: Hash
    source_range: SourceRange


class SubmitDraft(Contract):
    tool: Literal['word.submit_draft'] = 'word.submit_draft'
    ref: str = Field(pattern=r'^df_[0-9a-f]{32}$')
    text: str = Field(min_length=1, max_length=8000)

    @field_validator('text')
    @classmethod
    def valid_text(cls, value):
        if not value.strip():
            raise ValueError('Draft text must not be blank.')
        value.encode('utf-8')
        return value


def normalize(args):
    model = SubmitDraft if args.get('tool') == 'word.submit_draft' or 'ref' in args else PlanDraft
    return model.model_validate(args).model_dump()


def _hash(value):
    return digest(canonical(value).encode('utf-8'))


def _read(store, key):
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if row is None:
        return None
    try:
        envelope = json.loads(row[0])
        if set(envelope) != {'state', 'hash'} or envelope['hash'] != _hash(envelope['state']):
            raise ValueError('envelope')
        return envelope['state']
    except (ValueError, TypeError, KeyError) as exc:
        raise HarnessError('state_corrupt', 'Draft ledger integrity check failed.') from exc


def _write(store, key, state):
    store.db.execute('INSERT INTO meta VALUES(?,?)',
                     (key, canonical({'state': state, 'hash': _hash(state)})))


def _key(task_id, ref, status):
    return HISTORY_PREFIX + task_id + ':' + ref + ':' + status


def _validate(state, owner, *, historical=False):
    try:
        fields = {'version', 'generation', 'ref', 'owner', 'request', 'request_hash',
                  'source_text', 'source_version', 'source_span', 'instruction',
                  'status', 'supersedes'}
        if state['status'] == 'drafted':
            fields |= {'text', 'pending_hash'}
        if set(state) != fields or type(state['version']) is not int or state['version'] != 1:
            raise ValueError('fields/version')
        if type(state['generation']) is not int or state['generation'] < 1:
            raise ValueError('generation')
        previous = state['supersedes']
        if state['generation'] == 1:
            if previous is not None:
                raise ValueError('first generation')
        elif (not isinstance(previous, dict) or set(previous) != {'generation', 'ref', 'state_hash'}
              or type(previous['generation']) is not int
              or previous['generation'] != state['generation'] - 1
              or not isinstance(previous['ref'], str)
              or not re.fullmatch(r'df_[0-9a-f]{32}', previous['ref'])
              or not isinstance(previous['state_hash'], str)
              or not re.fullmatch(r'[0-9a-f]{64}', previous['state_hash'])):
            raise ValueError('supersedes')
        if not re.fullmatch(r'df_[0-9a-f]{32}', state['ref']):
            raise ValueError('ref')
        request = PlanDraft.model_validate(state['request']).model_dump()
        if request != state['request'] or _hash(request) != state['request_hash']:
            raise ValueError('request')
        selected = request['source_range']
        span = resolve_text(state['source_text'], request['source_hash'],
                            selected['start'], selected['end'], version=state['source_version'])
        if state['instruction'] != span['text'] or state['source_span'] != {'start': span['start'], 'end': span['end']}:
            raise ValueError('source span')
        if state['status'] == 'drafted':
            SubmitDraft(ref=state['ref'], text=state['text'])
        elif state['status'] != 'pending':
            raise ValueError('status')
        if set(state['owner']) != {'task_id', 'chat_id', 'target'}:
            raise ValueError('owner')
        keys = ('task_id', 'chat_id') if historical else ('task_id', 'chat_id', 'target')
        if any(state['owner'][key] != owner[key] for key in keys):
            raise HarnessError('draft_scope', 'Draft belongs to another task, chat or target.')
    except HarnessError as exc:
        if exc.code == 'draft_scope':
            raise
        raise HarnessError('state_corrupt', 'Draft source evidence changed.') from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('state_corrupt', 'Draft evidence integrity check failed.') from exc


def _state(store, task_id, ref, status, owner, *, historical=False):
    state = _read(store, _key(task_id, ref, status))
    if state is None:
        raise HarnessError('draft_reference', 'Draft reference is unavailable in this task.')
    _validate(state, owner, historical=historical)
    if state['ref'] != ref or state['status'] != status:
        raise HarnessError('state_corrupt', 'Draft history identity changed.')
    previous = state['supersedes']
    if previous is not None:
        found = False
        for prior_status in ('pending', 'drafted'):
            prior = _read(store, _key(task_id, previous['ref'], prior_status))
            if prior is not None and _hash(prior) == previous['state_hash']:
                _validate(prior, owner, historical=historical)
                found = (prior['ref'] == previous['ref'] and prior['status'] == prior_status
                         and prior['generation'] == previous['generation'])
                break
        if not found:
            raise HarnessError('state_corrupt', 'Draft predecessor evidence changed.')
    if status == 'drafted':
        original = _state(store, task_id, ref, 'pending', owner, historical=historical)
        expected = {**original, 'status': 'drafted', 'text': state['text'], 'pending_hash': _hash(original)}
        if state != expected:
            raise HarnessError('state_corrupt', 'Draft pending evidence changed.')
    return state


def _get(store, task_id, owner):
    pointer = _read(store, PREFIX + task_id)
    if pointer is None:
        return None
    try:
        if set(pointer) != {'ref', 'status', 'state_hash'}:
            raise ValueError('pointer')
        state = _state(store, task_id, pointer['ref'], pointer['status'], owner)
        if pointer['state_hash'] != _hash(state):
            raise ValueError('hash')
        return state
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('state_corrupt', 'Draft active pointer changed.') from exc


def get(store, task_id, ref=None):
    """Verified active state, or an owned reference; does not assert freshness."""
    with store.transaction():
        owner = _plan_owner(store, task_id)
        if ref is None:
            return _get(store, task_id, owner)
        SubmitDraft(ref=ref, text='validate reference')
        status = 'drafted' if _read(store, _key(task_id, ref, 'drafted')) is not None else 'pending'
        return _state(store, task_id, ref, status, owner)


def pending(store, task_id):
    """Current pending generation, or None; source freshness is checked on submit."""
    state = get(store, task_id)
    return state if state is not None and state['status'] == 'pending' else None


def _call(store, task_id, call_id, request, owner):
    if not isinstance(call_id, str) or not re.fullmatch(r'call_[0-9a-f]{32}', call_id):
        raise HarnessError('native_history', 'Expected a host-issued draft call identity.')
    row = store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                           (call_id, task_id, owner['chat_id'])).fetchone()
    if row is None:
        raise HarnessError('native_history', 'Draft call belongs to another task or chat.')
    try:
        function = json.loads(row['function'])
        if function['name'] != request['tool'] or normalize(function['arguments']) != request:
            raise ValueError('arguments')
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('native_history', 'Draft call arguments differ.') from exc
    return row


def _receipt(state):
    return {'status': state['status'], 'ref': state['ref'], 'generation': state['generation'],
            'source_hash': state['request']['source_hash'], 'document_changed': False,
            'supersedes_generation': state['supersedes']['generation'] if state['supersedes'] else None,
            'interpretation': 'model_selected', 'semantic_validation': 'unassessed'}


def _result(store, task_id, call_id, request, owner):
    _call(store, task_id, call_id, request, owner)
    saved = _read(store, RESULT_PREFIX + call_id)
    if saved is None:
        return None
    try:
        if set(saved) != {'call_id', 'request', 'state_hash', 'receipt'}:
            raise ValueError('fields')
        if saved['call_id'] != call_id or saved['request'] != request:
            raise ValueError('identity')
        status = 'pending' if request['tool'] == 'word.plan_draft' else 'drafted'
        state = _state(store, task_id, saved['receipt']['ref'], status, owner, historical=True)
        if saved['state_hash'] != _hash(state) or saved['receipt'] != _receipt(state):
            raise ValueError('receipt')
        expected = state['request'] if status == 'pending' else SubmitDraft(ref=state['ref'], text=state['text']).model_dump()
        if request != expected:
            raise ValueError('request')
        return saved['receipt']
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError('state_corrupt', 'Draft call receipt changed.') from exc


def result(store, task_id, args, *, native_call_id):
    """Exact historical call evidence only; never activates or accepts a draft."""
    request = normalize(args)
    with store.transaction():
        return _result(store, task_id, native_call_id, request, _plan_owner(store, task_id))


def _replay(store, task_id, call_id, request, owner):
    if call_id is None:
        return None
    receipt = _result(store, task_id, call_id, request, owner)
    if receipt is None and _call(store, task_id, call_id, request, owner)['result_message'] is not None:
        raise HarnessError('native_history', 'Completed call has no draft receipt.')
    return receipt


def _save(store, task_id, state, request, call_id):
    _write(store, _key(task_id, state['ref'], state['status']), state)
    store.db.execute('DELETE FROM meta WHERE key=?', (PREFIX + task_id,))
    _write(store, PREFIX + task_id, {'ref': state['ref'], 'status': state['status'], 'state_hash': _hash(state)})
    return _save_result(store, state, request, call_id)


def _save_result(store, state, request, call_id):
    receipt = _receipt(state)
    if call_id is not None:
        _write(store, RESULT_PREFIX + call_id, {'call_id': call_id, 'request': request,
                                             'state_hash': _hash(state), 'receipt': receipt})
    return receipt


def plan_draft(store, task_id, *, source_hash, source_range, native_call_id=None):
    request = PlanDraft(source_hash=source_hash, source_range=source_range).model_dump()
    with store.transaction():
        owner = _plan_owner(store, task_id)
        prior = _replay(store, task_id, native_call_id, request, owner)
        if prior is not None:
            return prior
        if not owner['target'] or not owner['target'].lower().endswith('.docx'):
            raise HarnessError('draft_scope', 'Draft requires an active Word target.')
        previous = _get(store, task_id, owner)
        _, objective, _ = source(store, task_id)
        selected = request['source_range']
        span = resolve_text(objective, source_hash, selected['start'], selected['end'])
        state = {'version': 1, 'generation': previous['generation'] + 1 if previous else 1,
                 'ref': 'df_' + uuid.uuid4().hex, 'owner': owner, 'request': request,
                 'request_hash': _hash(request), 'source_text': objective, 'source_version': VERSION,
                 'source_span': {'start': span['start'], 'end': span['end']}, 'instruction': span['text'],
                 'status': 'pending', 'supersedes': {'generation': previous['generation'],
                     'ref': previous['ref'], 'state_hash': _hash(previous)} if previous else None}
        return _save(store, task_id, state, request, native_call_id)


def submit_draft(store, task_id, *, ref, text, native_call_id=None):
    request = SubmitDraft(ref=ref, text=text).model_dump()
    with store.transaction():
        owner = _plan_owner(store, task_id)
        prior = _replay(store, task_id, native_call_id, request, owner)
        if prior is not None:
            return prior
        state = _get(store, task_id, owner)
        if state is None or state['ref'] != ref:
            raise HarnessError('draft_stale', 'Submit only the current draft generation.')
        _, objective, source_hash = source(store, task_id)
        if source_hash != state['request']['source_hash'] or objective != state['source_text']:
            raise HarnessError('draft_stale', 'Draft instruction source changed.')
        if state['status'] == 'drafted':
            if state['text'] != text:
                raise HarnessError('draft_conflict', 'Draft reference already contains different text.')
            return _save_result(store, state, request, native_call_id)
        drafted = {**state, 'status': 'drafted', 'text': text, 'pending_hash': _hash(state)}
        return _save(store, task_id, drafted, request, native_call_id)
