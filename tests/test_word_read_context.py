"""SQLite-only read-context tests: no document worker, renderer or model needed."""
import copy
import json

import pytest

from localoffice.chats import current_chat, create_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word.evidence import issue
from localoffice.word.read_context import PREFIX, remember, summary, MAX_RECORD_BYTES


def setup(root, artifact='report.docx'):
    store = ProjectStore(root)
    chat = current_chat(store)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('task', 'Read the current footer', 'running', 'background', artifact, 1, 1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)', ('task', chat))
    # Only a hash fixture: these tests never parse or claim this is a valid DOCX.
    (root/'report.docx').write_bytes(b'current revision fixture')
    return store, store.file_hash('report.docx')


def issued(store, revision, identity='paragraph:footer:1:1', text='Page of1 of 2', *,
           offset=0, total=None, story=None, span=None, path='report.docx', task_id='task'):
    obj = {'id':identity, 'hash':digest((identity+revision).encode()), 'text':text,
           'total_chars':len(text)+offset if total is None else total}
    obj['excerpt_offset' if span is not None else 'offset'] = offset
    if span is not None:
        obj.update(start=span[0], end=span[1])
    if story is not None:
        obj['story'] = story
    return issue(store, task_id, {'path':path, 'revision':revision,
                 'matches' if span is not None else 'objects':[obj]})


def record(store):
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX+'task',)).fetchone()
    return json.loads(row[0]) if row else None


def corrupt(store, change, *, rehash=False):
    envelope = record(store)
    change(envelope)
    if rehash:
        envelope['hash'] = digest(canonical(envelope['state']).encode())
    with store.transaction():
        store.db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(envelope), PREFIX+'task'))


def test_footer_survives_unrelated_reads_restart_and_history_changes(tmp_path):
    store, rev = setup(tmp_path)
    try:
        footer = issued(store, rev, span=(0, 7), story={
            'kind':'footer', 'contains_fields':True, 'root_id':'footer:1',
            'interpretation':'Do not persist this large commentary'})
        original = copy.deepcopy(footer)
        assert remember(store, 'task', footer) is footer
        assert footer == original
        for number in (1, 2):
            remember(store, 'task', issued(store, rev, f'paragraph:{number}', f'Body {number}'))
        before = summary(store, 'task', 'report.docx', rev)
        assert len(before['objects']) == 3 and before['truncated'] is False
        fact = next(x for x in before['objects'] if x['target'] == 'paragraph:footer:1:1')
        assert fact == {'target':'paragraph:footer:1:1', 'expected_hash':footer['matches'][0]['evidence_ref'],
                        'text':'Page of1 of 2', 'offset':0, 'total_chars':13, 'truncated':False,
                        'start':0, 'end':7, 'story':{'kind':'footer', 'contains_fields':True}}
        store.message('assistant', 'Invented paragraph:999 and a fake footer hash', 'task')
        store.close(); store = ProjectStore(tmp_path)
        assert summary(store, 'task', 'report.docx', rev) == before
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        assert (tmp_path/'report.docx').read_bytes() == b'current revision fixture'
    finally:
        store.close()


def test_three_most_recent_unique_objects_and_reread_replaces_excerpt(tmp_path):
    store, rev = setup(tmp_path)
    try:
        for number in (1, 2, 3):
            remember(store, 'task', issued(store, rev, f'paragraph:{number}', 'abcdef', total=6))
        remember(store, 'task', issued(store, rev, 'paragraph:1', 'def', offset=3, total=6))
        remember(store, 'task', issued(store, rev, 'paragraph:4', 'four'))
        result = summary(store, 'task', 'report.docx', rev)
        assert [x['target'] for x in result['objects']] == ['paragraph:4', 'paragraph:1', 'paragraph:3']
        reread = result['objects'][1]
        assert reread['text'] == 'def' and reread['offset'] == 3 and reread['truncated'] is True
        assert 'start' not in reread and 'end' not in reread
    finally:
        store.close()


def test_external_read_empty_read_and_preview_do_not_evict_footer(tmp_path):
    store, rev = setup(tmp_path)
    try:
        remember(store, 'task', issued(store, rev))
        before = record(store)
        external = issued(store, rev, path='source.docx')
        assert remember(store, 'task', external) is external
        preview = issue(store, 'task', {'path':'report.docx', 'revision':rev,
            'objects':[{'id':'paragraph:2', 'hash':'a'*64, 'preview':'Not a complete read'}]})
        remember(store, 'task', preview)
        remember(store, 'task', {'path':'report.docx', 'revision':rev, 'matches':[]})
        assert record(store) == before
    finally:
        store.close()


def test_no_artifact_or_no_task_returns_input_unchanged_without_record(tmp_path):
    store, rev = setup(tmp_path, artifact=None)
    try:
        result = issued(store, rev)
        assert remember(store, 'task', result) is result
        assert remember(store, None, result) is result
        assert record(store) is None
        assert summary(store, 'task', 'report.docx', rev) is None
    finally:
        store.close()


def test_unissued_and_missing_excerpt_facts_are_not_inferred(tmp_path):
    store, rev = setup(tmp_path)
    try:
        raw = {'path':'report.docx', 'revision':rev, 'objects':[
            {'id':'paragraph:1', 'hash':'a'*64, 'text':'Guess', 'offset':0, 'total_chars':5}]}
        remember(store, 'task', raw)
        assert record(store) is None
        result = issued(store, rev)
        result['objects'][0].pop('offset')
        # Clear a record potentially created by coordinator's evidence.issue hook.
        with store.transaction():store.db.execute('DELETE FROM meta WHERE key=?', (PREFIX+'task',))
        remember(store, 'task', result)
        assert record(store) is None
    finally:
        store.close()


def test_new_revision_resets_set_and_old_revision_or_retarget_is_omitted(tmp_path):
    store, rev = setup(tmp_path)
    try:
        first = issued(store, rev)
        remember(store, 'task', first)
        (tmp_path/'report.docx').write_bytes(b'new current revision')
        current = store.file_hash('report.docx')
        assert summary(store, 'task', 'report.docx', rev) is None
        assert summary(store, 'task', 'report.docx', current) is None
        before = record(store)
        remember(store, 'task', first)
        assert record(store) == before
        remember(store, 'task', issued(store, current, 'paragraph:3', 'New'))
        assert [x['target'] for x in summary(store, 'task', 'report.docx', current)['objects']] == ['paragraph:3']
        assert summary(store, 'task', 'source.docx', current) is None
        with store.transaction():store.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
        assert summary(store, 'task', 'report.docx', current) is None
    finally:
        store.close()


def test_candidate_revision_is_authoritative_over_saved_file(tmp_path):
    store, rev = setup(tmp_path)
    try:
        remember(store, 'task', issued(store, rev))
        new = store.put_blob(b'working copy revision')
        with store.transaction():
            store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)', ('report.docx', 'fixture-op', rev, new))
        assert summary(store, 'task', 'report.docx', rev) is None
        remember(store, 'task', issued(store, new, 'paragraph:1', 'Candidate'))
        assert summary(store, 'task', 'report.docx', new)['revision'] == new
        assert (tmp_path/'report.docx').read_bytes() == b'current revision fixture'
    finally:
        store.close()


def test_unicode_excerpt_counts_codepoints_and_retains_actual_global_match_span(tmp_path):
    store, rev = setup(tmp_path)
    try:
        text = '\U0001f600'*125+'e\u0301'
        obj = issued(store, rev, text=text, offset=40, total=500, span=(150, 166))
        remember(store, 'task', obj)
        item = record(store)['state']['objects'][0]
        assert item['text'] == text[:120] and item['offset'] == 40
        assert item['total_chars'] == 500 and item['truncated'] is True
        # An actual match need not fit wholly into our shorter retained excerpt.
        assert (item['start'], item['end']) == (150, 166)
        result = summary(store, 'task', 'report.docx', rev)
        assert len(canonical(result).encode()) <= 800
        if result['objects']:
            assert result['objects'][0]['text'] == text[:120]
    finally:
        store.close()


@pytest.mark.parametrize('budget', [128, 256, 500, 800, 2000])
def test_summary_byte_budget_omits_whole_items_without_fabricating_shorter_reads(tmp_path, budget):
    store, rev = setup(tmp_path)
    try:
        for index in range(3):
            remember(store, 'task', issued(store, rev, f'paragraph:{index}', '\U0001f600'*120))
        result = summary(store, 'task', 'report.docx', rev, max_bytes=budget)
        assert len(canonical(result).encode()) <= min(budget, 800)
        assert result['truncated'] == (len(result['objects']) < 3)
        assert all(x['text'] == '\U0001f600'*120 for x in result['objects'])
        raw = store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX+'task',)).fetchone()[0]
        assert len(raw.encode()) <= MAX_RECORD_BYTES
    finally:
        store.close()


def test_record_byte_budget_evicts_oldest_without_cutting_identity(tmp_path):
    store, rev = setup(tmp_path)
    try:
        # XML allows tabs; JSON encoding makes each tab cost two bytes.
        # Long IDs and a long active path exercise the durable envelope limit.
        path = 'a'*240+'.docx'
        (tmp_path/path).write_bytes(b'long-path fixture')
        with store.transaction():store.db.execute('UPDATE tasks SET artifact=? WHERE id=?', (path, 'task'))
        revision = store.file_hash(path)
        for index in range(3):
            identity = str(index)+'x'*159
            remember(store, 'task', issued(store, revision, identity, '\t'*120, path=path))
        raw = store.db.execute('SELECT value FROM meta WHERE key=?', (PREFIX+'task',)).fetchone()[0]
        assert len(raw.encode()) <= MAX_RECORD_BYTES
        assert record(store)['state']['objects'][-1]['id'] == '2'+'x'*159
    finally:
        store.close()


@pytest.mark.parametrize('change', ['hash', 'shape', 'scope', 'duplicate', 'span', 'excerpt', 'truncated'])
def test_corrupt_envelope_fails_closed_even_when_requested_revision_is_stale(tmp_path, change):
    store, rev = setup(tmp_path)
    try:
        remember(store, 'task', issued(store, rev))
        def mutate(envelope):
            state = envelope['state']; item = state['objects'][0]
            if change == 'hash':item['text'] = 'different'
            elif change == 'shape':state['unexpected'] = True
            elif change == 'scope':state['chat_id'] = 'another-chat'
            elif change == 'duplicate':state['objects'].append(copy.deepcopy(item))
            elif change == 'span':item.update(start=0)
            elif change == 'excerpt':item['offset'] = item['total_chars']+1
            else:item['truncated'] = not item['truncated']
        corrupt(store, mutate, rehash=change != 'hash')
        with pytest.raises(HarnessError) as error:
            summary(store, 'task', 'report.docx', '0'*64)
        assert error.value.code == 'state_corrupt'
    finally:
        store.close()


@pytest.mark.parametrize('change', ['missing', 'hash', 'path', 'object', 'revision'])
def test_evidence_binding_changes_fail_closed(tmp_path, change):
    store, rev = setup(tmp_path)
    try:
        remember(store, 'task', issued(store, rev))
        ref = record(store)['state']['objects'][0]['evidence_ref']
        with store.transaction():
            if change == 'missing':store.db.execute('DELETE FROM word_evidence WHERE ref=?', (ref,))
            else:
                column = {'hash':'object_hash', 'path':'path', 'object':'object_id', 'revision':'revision'}[change]
                value = {'hash':'0'*64, 'path':'other.docx', 'object':'paragraph:99', 'revision':'0'*64}[change]
                store.db.execute(f'UPDATE word_evidence SET {column}=? WHERE ref=?', (value, ref))
        with pytest.raises(HarnessError) as error:
            summary(store, 'task', 'report.docx', rev)
        assert error.value.code == 'state_corrupt'
    finally:
        store.close()


def test_wrong_chat_and_unknown_task_cannot_read_or_write_context(tmp_path):
    store, rev = setup(tmp_path)
    try:
        result = issued(store, rev); remember(store, 'task', result)
        before = record(store)
        for action in (lambda:summary(store, 'unknown', 'report.docx', rev),
                       lambda:remember(store, 'unknown', result)):
            with pytest.raises(HarnessError):action()
        select_chat(store, create_chat(store))
        for action in (lambda:summary(store, 'task', 'report.docx', rev),
                       lambda:remember(store, 'task', result)):
            with pytest.raises(HarnessError) as error:action()
            assert error.value.code == 'read_context_scope'
        assert record(store) == before
    finally:
        store.close()


def test_unissued_reference_and_changed_payload_hash_never_replace_good_state(tmp_path):
    store, rev = setup(tmp_path)
    try:
        result = issued(store, rev); remember(store, 'task', result)
        before = record(store)
        for field, value in [('evidence_ref', 'ev_'+'0'*16), ('hash', '0'*64)]:
            altered = copy.deepcopy(result); altered['objects'][0][field] = value
            with pytest.raises(HarnessError):remember(store, 'task', altered)
            assert record(store) == before
    finally:
        store.close()


def test_preferred_footer_match_survives_later_root_read_under_480_bytes(tmp_path):
    store, rev = setup(tmp_path)
    try:
        footer = issued(store, rev, span=(0, 7), story={'kind':'footer', 'contains_fields':True})
        remember(store, 'task', footer)
        remember(store, 'task', issued(store, rev, 'paragraph:1', 'First page'))
        remember(store, 'task', issued(store, rev, 'header:1', 'Quarterly report', story={'kind':'header', 'contains_fields':False}))
        result = summary(store, 'task', 'report.docx', rev, max_bytes=480, preferred_roles=('footer',))
        assert len(canonical(result).encode()) <= 480
        assert result['revision'] == rev and 'path' not in result
        fact = result['objects'][0]
        assert fact['target'] == footer['matches'][0]['id']
        assert fact['expected_hash'] == footer['matches'][0]['evidence_ref']
        assert (fact['start'], fact['end']) == (0, 7)
        assert result['truncated'] is True
        assert len(record(store)['state']['objects']) == 3
    finally:
        store.close()


def test_actual_match_precedes_root_and_preferred_roles_do_not_invent_objects(tmp_path):
    store, rev = setup(tmp_path)
    try:
        remember(store, 'task', issued(store, rev, 'paragraph:footer:1:1', 'Page ', span=(0, 5),
                                      story={'kind':'footer', 'contains_fields':False}))
        remember(store, 'task', issued(store, rev, 'footer:1', 'Page ',
                                      story={'kind':'footer', 'contains_fields':False}))
        result = summary(store, 'task', 'report.docx', rev, preferred_roles=('footer',))
        assert [x['target'] for x in result['objects']] == ['paragraph:footer:1:1', 'footer:1']
        assert 'start' not in result['objects'][1]
        absent = summary(store, 'task', 'report.docx', rev, preferred_roles=('header',))
        assert {x['target'] for x in absent['objects']} == {'paragraph:footer:1:1', 'footer:1'}
    finally:
        store.close()
