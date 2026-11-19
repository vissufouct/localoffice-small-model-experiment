"""Revision-bound user annotations and deterministic Markdown edit boundaries."""
import difflib
import json
import time
import uuid
from .errors import HarnessError
from .paths import project_path
from .storage import canonical


def annotate(store, *, path, revision, scope, locator, comment):
    project_path(store.root, path)
    if scope not in ('selection', 'artifact', 'page'):
        raise HarnessError('invalid_scope', 'Choose a selection, whole artifact or PDF page.')
    candidate = store.candidate(path)
    current = candidate['candidate_hash'] if candidate else store.file_hash(path)
    if current is None or current != revision:
        raise HarnessError('stale_revision', 'Refresh the preview before annotating.')
    normalized = {}
    if path.lower().endswith('.docx'):
        if scope=='page':raise HarnessError('invalid_scope','Use a structural Word object or explicit whole-artifact scope.')
        from .word.plugin import WordPlugin
        plugin=WordPlugin(store)
        if scope=='selection':
            object_id=locator.get('object_id');object_hash=locator.get('object_hash')
            if not isinstance(object_id,str) or not isinstance(object_hash,str):
                raise HarnessError('invalid_locator','Select a Word structural object before annotating.')
            result=plugin.read_objects(path=path,revision=revision,ids=[object_id],max_chars=4000)
            obj=result['objects'][0]
            if obj['hash']!=object_hash:raise HarnessError('stale_annotation','The selected Word object changed.')
            if not object_id.startswith(('paragraph:','image:','table:','cell:','row:')):
                raise HarnessError('scope_unsupported','Select a Word paragraph, image, table, row or cell; other object selection types are not yet supported.')
            normalized={'kind':'target','format':'docx','object_id':object_id,'object_hash':object_hash,
                        'quote':obj['text'],'quote_truncated':obj['next_offset'] is not None}
        else:normalized={'kind':'target','format':'docx'}
        store.put_blob(plugin._bytes(path,revision))
    elif path.lower().endswith('.pdf'):

        page = locator.get('page')
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise HarnessError('invalid_locator', 'PDF annotations require a positive page number.')
        normalized = {'page': page, 'quote': str(locator.get('quote', ''))[:8000], 'kind': 'source'}
        if scope == 'selection' and not normalized['quote']:
            raise HarnessError('invalid_locator', 'Select PDF text before annotating.')
    else:
        if scope == 'page':
            raise HarnessError('invalid_scope', 'Page scope is available for PDF sources.')
        content = store.read(path)
        if scope == 'selection':
            quote = locator.get('quote')
            if not isinstance(quote, str) or not quote:
                raise HarnessError('invalid_locator', 'Select a nonempty text span.')
            # A trusted structural selection may carry explicit Unicode codepoint offsets.
            # Preview quote-only selection must be unique; never silently choose the first match.
            if locator.get('offset_encoding') == 'unicode_codepoints':
                start, end = locator.get('start'), locator.get('end')
                if any(not isinstance(n, int) or isinstance(n, bool) for n in [start, end]):
                    raise HarnessError('invalid_locator', 'Selection offsets must be integers.')
                if not 0 <= start < end <= len(content['text']) or content['text'][start:end] != quote:
                    raise HarnessError('invalid_locator', 'Selection does not match the displayed revision.')
            else:
                if content['text'].count(quote) != 1:
                    raise HarnessError('ambiguous_locator', 'Selected text must identify one exact Markdown span. Use structural offsets or explicitly broaden scope.')
                start = content['text'].index(quote)
                end = start + len(quote)
            normalized = {'start': start, 'end': end, 'quote': quote, 'offset_encoding': 'unicode_codepoints', 'kind': 'target'}
        else:
            normalized = {'kind': 'target'}
        store.put_blob(content['text'].encode())
    key = str(uuid.uuid4())
    with store.transaction():
        store.db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?)',
                         (key, path, revision, scope, canonical(normalized), comment, time.time()))
        from .chats import current_chat
        store.db.execute('INSERT INTO annotation_chats VALUES(?,?)',(key,current_chat(store)))
    return key


def bind_annotations(store, task_id, artifact, annotation_ids):
    """Called only from a user task request, never from model-proposed tool arguments."""
    if len(annotation_ids) > 16 or len(set(annotation_ids)) != len(annotation_ids):
        raise HarnessError('annotation_limit', 'Attach up to 16 distinct annotations per request.')
    target = []
    for key in annotation_ids:
        row = store.db.execute('SELECT * FROM annotations WHERE id=?', (key,)).fetchone()
        if not row:
            raise HarnessError('not_found', 'Annotation not found in this project.')
        from .chats import current_chat
        annotation_chat=store.db.execute('SELECT chat_id FROM annotation_chats WHERE annotation_id=?',(key,)).fetchone()
        if not annotation_chat or annotation_chat[0]!=current_chat(store):
            raise HarnessError('chat_scope','An annotation must belong to the active chat.')
        candidate = store.candidate(row['path'])
        current = candidate['candidate_hash'] if candidate else store.file_hash(row['path'])
        if current != row['revision']:
            raise HarnessError('stale_annotation', 'An attached annotation refers to an older revision. Reattach it to the current preview.')
        if not row['path'].lower().endswith('.pdf'):
            if row['path'] != artifact:
                raise HarnessError('artifact_scope', 'A target annotation must belong to the active artifact.')
            target.append(row)
        store.db.execute('INSERT OR IGNORE INTO task_annotations VALUES(?,?)', (task_id, key))
    if target and artifact.lower().endswith('.docx'):
        whole=any(row['scope']=='artifact' for row in target)
        objects=[json.loads(row['locator'])['object_id'] for row in target if row['scope']=='selection']
        revision=target[0]['revision']
        store.db.execute('INSERT OR REPLACE INTO task_scopes VALUES(?,?,?,?,?)',
                         (task_id,artifact,revision,revision,canonical(None if whole else {'format':'docx','objects':objects})))
    elif target:
        content = store.read(artifact)
        ranges = []
        whole = any(row['scope'] == 'artifact' for row in target)
        for row in target:
            if row['scope'] == 'selection':
                locator = json.loads(row['locator'])
                ranges.append([locator['start'], locator['end']])
        store.put_blob(content['text'].encode())
        store.db.execute('INSERT OR REPLACE INTO task_scopes VALUES(?,?,?,?,?)',
                         (task_id, artifact, content['revision'], content['revision'], canonical(None if whole else ranges)))


def validate_patch_scope(store, task_id, path, expected_revision, text):
    if not task_id:
        return
    task = store.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task or task['status'] != 'running':
        raise HarnessError('task_state', 'A model mutation requires a running host task.')
    if task['artifact'] and task['artifact'] != path:
        raise HarnessError('artifact_scope', 'Only the active artifact can be edited.')
    scope = store.db.execute('SELECT * FROM task_scopes WHERE task_id=?', (task_id,)).fetchone()
    if not scope:
        return
    if scope['path'] != path or scope['head_revision'] != expected_revision:
        raise HarnessError('stale_annotation', 'The task scope does not match the current working revision.')
    ranges = json.loads(scope['ranges'])
    if ranges is None:
        return  # User explicitly chose the entire artifact.
    before = store.blob(scope['base_revision']).decode()
    if len(ranges) == 1:
        start, end = ranges[0]
        prefix, suffix = before[:start], before[end:]
        if len(text) < len(prefix) + len(suffix) or not text.startswith(prefix) or not text.endswith(suffix):
            raise HarnessError('scope_violation', 'The edit changes text outside the selected span. Request broader scope explicitly.')
        return
    # Multiple selections: every changed interval must fit a declared region.
    for tag, left, right, _, _ in difflib.SequenceMatcher(None, before, text, autojunk=True).get_opcodes():
        if tag != 'equal' and not any(start <= left <= right <= end for start, end in ranges):
            raise HarnessError('scope_violation', 'The edit changes text outside the selected spans.')


def validate_word_scope(store,task_id,path,revision,operations):
    """Enforce host-bound object identities before any Word worker mutation."""
    if not task_id:return
    task=store.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task or task['status']!='running':raise HarnessError('task_state','Word mutations require a running task.')
    if task['artifact']!=path:raise HarnessError('artifact_scope','Only the active Word artifact can be edited.')
    scope=store.db.execute('SELECT * FROM task_scopes WHERE task_id=?',(task_id,)).fetchone()
    if not scope:return
    if scope['path']!=path or scope['head_revision']!=revision:
        raise HarnessError('stale_annotation','Word annotation scope does not match the current revision.')
    bounds=json.loads(scope['ranges'])
    if bounds is None:return
    if not isinstance(bounds,dict) or bounds.get('format')!='docx':
        raise HarnessError('scope_violation','The task has no compatible Word object scope.')
    if any(op.op in ('insert_paragraphs','delete_paragraph','insert_rows','insert_table','merge_cells','unmerge_cells','insert_image','create_paragraph_style','create_character_style') for op in operations):
        raise HarnessError('scope_violation','Structural changes or style creation require explicit whole-artifact scope.')
    # Replacing a cell can change paragraph counts and therefore paragraph locators.
    # Keep a mixed selection from silently authorizing a different paragraph afterwards.
    if any(op.op=='set_cell_text' for op in operations) and any(obj.startswith('paragraph:') for obj in bounds['objects']):
        raise HarnessError('scope_violation','Replace cell text in a separate cell-scoped task, then refresh paragraph annotations, or explicitly choose whole-artifact scope.')
    if not operations or any(op.target not in bounds['objects'] for op in operations):
        raise HarnessError('scope_violation','The edit targets a Word object outside the annotated selection.')
