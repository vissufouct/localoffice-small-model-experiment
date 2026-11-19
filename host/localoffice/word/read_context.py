"""Bounded issued-read working set; never infer targets from conversational history.

Call remember after evidence.issue. Text is trusted only as an actual worker read
payload supplied by that caller; evidence.resolve binds identity/hash, not text.
"""
import json
import re

from ..chats import current_chat
from ..errors import HarnessError
from ..paths import project_path
from ..storage import canonical, digest
from .evidence import resolve

PREFIX = 'word_read_context:'
MAX_RECORD_BYTES = 4096
MAX_ITEMS = 3
MAX_TEXT = 120
HASH = re.compile(r'[0-9a-f]{64}\Z')
REF = re.compile(r'ev_[0-9a-f]{16}\Z')


def _require(ok, message, code='state_corrupt'):
    if not ok:
        raise HarnessError(code, message)


def _owner(store, task_id):
    row = store.db.execute('''SELECT t.artifact,c.chat_id FROM tasks t
        JOIN task_chats c ON c.task_id=t.id WHERE t.id=?''', (task_id,)).fetchone()
    _require(row is not None and row['chat_id'] == current_chat(store),
             'Read context belongs to its original task and chat.', 'read_context_scope')
    return dict(row)


def _integer(value):
    return type(value) is int and value >= 0


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _text(value):
    return isinstance(value, str) and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def _validate_item(item):
    required = {'id', 'evidence_ref', 'object_hash', 'text', 'offset', 'total_chars', 'truncated'}
    _require(isinstance(item, dict) and required <= item.keys() and
             not item.keys() - required - {'start', 'end', 'story'}, 'Invalid read-context item fields.')
    _require(_text(item['id']) and 1 <= len(item['id']) <= 160 and
             isinstance(item['evidence_ref'], str) and REF.fullmatch(item['evidence_ref']) and
             _hash(item['object_hash']), 'Invalid read-context identity.')
    _require(_text(item['text']) and len(item['text']) <= MAX_TEXT and
             _integer(item['offset']) and _integer(item['total_chars']) and
             item['offset'] + len(item['text']) <= item['total_chars'], 'Invalid read-context excerpt.')
    _require(type(item['truncated']) is bool and item['truncated'] ==
             (item['offset'] != 0 or len(item['text']) != item['total_chars']),
             'Invalid read-context truncation flag.')
    _require(('start' in item) == ('end' in item), 'Incomplete read-context match span.')
    if 'start' in item:
        _require(_integer(item['start']) and _integer(item['end']) and
                 item['start'] < item['end'] <= item['total_chars'], 'Invalid read-context match span.')
    if 'story' in item:
        story = item['story']
        _require(isinstance(story, dict) and bool(story) and
                 not story.keys() - {'kind', 'contains_fields'}, 'Invalid read-context story metadata.')
        if 'kind' in story:
            _require(story['kind'] in ('header', 'footer'), 'Invalid read-context story kind.')
        if 'contains_fields' in story:
            _require(type(story['contains_fields']) is bool, 'Invalid read-context field flag.')


def _load(store, task_id, owner):
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX+task_id,)).fetchone()
    if row is None:
        return None
    try:
        raw = row[0]
        _require(isinstance(raw, str) and len(raw.encode()) <= MAX_RECORD_BYTES,
                 'Read-context record exceeds its byte bound.')
        envelope = json.loads(raw)
        _require(isinstance(envelope, dict) and set(envelope) == {'state', 'hash'},
                 'Invalid read-context envelope.')
        state = envelope['state']
        _require(_hash(envelope['hash']) and envelope['hash'] == digest(canonical(state).encode()),
                 'Read-context integrity hash mismatch.')
        _require(isinstance(state, dict) and set(state) ==
                 {'version', 'task_id', 'chat_id', 'path', 'revision', 'objects'}, 'Invalid read-context state.')
        _require(type(state['version']) is int and state['version'] == 1 and
                 state['task_id'] == task_id and state['chat_id'] == owner['chat_id'],
                 'Read-context envelope ownership mismatch.')
        _require(_text(state['path']) and 1 <= len(state['path']) <= 1024 and
                 state['path'].lower().endswith('.docx') and _hash(state['revision']),
                 'Invalid read-context document identity.')
        project_path(store.root, state['path'])
        items = state['objects']
        _require(isinstance(items, list) and 1 <= len(items) <= MAX_ITEMS, 'Invalid read-context item count.')
        for item in items:
            _validate_item(item)
        _require(len({item['id'] for item in items}) == len(items), 'Duplicate read-context object IDs.')
        return state
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise HarnessError('state_corrupt', 'Malformed read-context record.') from exc


def _verify(store, task_id, state, item):
    try:
        actual = resolve(store, task_id, state['path'], state['revision'], item['id'], item['evidence_ref'])
    except HarnessError as exc:
        if exc.code != 'stale_evidence':
            raise
        raise HarnessError('state_corrupt', 'Read-context evidence is missing or has a different scope.') from exc
    _require(actual == item['object_hash'], 'Read-context evidence hash changed.')


def _current(store, path):
    project_path(store.root, path)
    candidate = store.candidate(path)
    return candidate['candidate_hash'] if candidate else store.file_hash(path)


def _capture(obj, match):
    # list_objects previews do not carry complete offset/length facts. Do not
    # promote them to read excerpts or invent offset zero for them.
    offset_key = 'excerpt_offset' if match else 'offset'
    if not isinstance(obj, dict) or 'evidence_ref' not in obj:
        return None
    if not {'text', offset_key, 'total_chars'} <= obj.keys():
        return None
    text, offset, total = obj['text'], obj[offset_key], obj['total_chars']
    _require(_text(text) and _integer(offset) and _integer(total) and offset+len(text) <= total,
             'Invalid issued read excerpt.', 'read_context_payload')
    excerpt = text[:MAX_TEXT]
    item = {'id':obj.get('id'), 'evidence_ref':obj['evidence_ref'], 'object_hash':obj.get('hash'),
            'text':excerpt, 'offset':offset, 'total_chars':total,
            'truncated':offset != 0 or len(excerpt) != total}
    if match:
        _require('start' in obj and 'end' in obj, 'Missing issued match span.', 'read_context_payload')
        item.update(start=obj['start'], end=obj['end'])
    elif 'start' in obj or 'end' in obj:
        _require('start' in obj and 'end' in obj, 'Incomplete issued match span.', 'read_context_payload')
        item.update(start=obj['start'], end=obj['end'])
    if 'story' in obj:
        _require(isinstance(obj['story'], dict), 'Invalid issued story metadata.', 'read_context_payload')
        story = {key:obj['story'][key] for key in ('kind', 'contains_fields') if key in obj['story']}
        if story:
            item['story'] = story
    _validate_item(item)
    return item


def remember(store, task_id, result):
    """Retain up to three recent unique objects; always return result unchanged.

    No-task reads, tasks without an artifact, external-source reads, list previews
    and reads without issued ev_ references do not populate the working set.
    """
    if task_id is None:
        return result
    with store.transaction():
        owner = _owner(store, task_id)
        prior = _load(store, task_id, owner)
        path = owner['artifact']
        if not path or not path.lower().endswith('.docx'):
            return result
        if not isinstance(result, dict) or result.get('path') != path:
            return result
        revision = result.get('revision')
        _require(_hash(revision), 'Issued read has no valid revision.', 'read_context_payload')
        if revision != _current(store, path):
            return result
        state = {'version':1, 'task_id':task_id, 'chat_id':owner['chat_id'],
                 'path':path, 'revision':revision, 'objects':[]}
        if prior and prior['path'] == path and prior['revision'] == revision:
            for item in prior['objects']:
                _verify(store, task_id, prior, item)
            state['objects'] = list(prior['objects'])
        _require(not ('objects' in result and 'matches' in result),
                 'Ambiguous issued read payload.', 'read_context_payload')
        match = 'matches' in result
        supplied = result.get('matches' if match else 'objects', [])
        _require(isinstance(supplied, list) and len(supplied) <= 50,
                 'Invalid or unbounded issued read objects.', 'read_context_payload')
        captured = False
        for obj in supplied:
            item = _capture(obj, match)
            if item is None:
                continue
            _verify(store, task_id, state, item)
            # Oldest to newest. A reread replaces only that object's observation;
            # unrelated objects at this same revision survive context recycling.
            state['objects'] = [old for old in state['objects'] if old['id'] != item['id']]
            state['objects'].append(item)
            state['objects'] = state['objects'][-MAX_ITEMS:]
            captured = True
        if captured:
            while state['objects']:
                envelope = {'state':state, 'hash':digest(canonical(state).encode())}
                encoded = canonical(envelope)
                if len(encoded.encode()) <= MAX_RECORD_BYTES:
                    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (PREFIX+task_id, encoded))
                    break
                state['objects'].pop(0)
            else:
                raise HarnessError('read_context_limit', 'Issued read cannot fit the bounded working set.')
    return result


def summary(store, task_id, path, revision, max_bytes=800, *, preferred_roles=()):
    """Project current issued facts, preferred role then match then recency.

    The caller supplies path; the revision is retained as binding evidence. Role
    priority is navigation only, not source-coverage certification. Story kinds
    come from issued metadata; body/cell priority uses the issued object kind.
    """
    if type(max_bytes) is not int or max_bytes < 128:
        raise ValueError('Read-context summary requires at least 128 bytes.')
    if (not isinstance(preferred_roles, (tuple, list)) or len(preferred_roles) > 4 or
            any(role not in ('paragraph', 'table_cell', 'header', 'footer') for role in preferred_roles)):
        raise ValueError('Choose up to four known read-context preferred roles.')
    budget = min(max_bytes, 800)
    with store.transaction():
        owner = _owner(store, task_id)
        state = _load(store, task_id, owner)
        if state is None:
            return None
        if path != owner['artifact'] or path != state['path'] or revision != state['revision']:
            return None
        if revision != _current(store, path):
            return None
        for item in state['objects']:
            _verify(store, task_id, state, item)
        def priority(pair):
            index, item = pair
            kind = item['id'].split(':', 1)[0]
            role = item.get('story', {}).get('kind')
            if role is None:
                role = {'paragraph':'paragraph', 'cell':'table_cell'}.get(kind)
            rank = preferred_roles.index(role) if role in preferred_roles else len(preferred_roles)
            detail = 0 if 'start' in item else 2 if kind in ('header', 'footer') else 1
            return rank, detail, -index
        ordered = sorted(enumerate(state['objects']), key=priority)
        result = {'revision':revision, 'objects':[], 'truncated':False}
        for _, item in ordered:
            fact = {'target':item['id'], 'expected_hash':item['evidence_ref'],
                    **{key:item[key] for key in ('text', 'offset', 'total_chars', 'truncated')}}
            for key in ('start', 'end', 'story'):
                if key in item:
                    fact[key] = item[key]
            proposed = {**result, 'objects':result['objects']+[fact]}
            if len(canonical(proposed).encode()) <= budget:
                result = proposed
        result['truncated'] = len(result['objects']) < len(state['objects'])
        return result
