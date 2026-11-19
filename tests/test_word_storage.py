"""Real binary staging, explicit commit, replay and restoration."""
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.errors import HarnessError
from test_word_worker import fixture_docx


def test_word_binary_stage_apply_undo_and_replay(tmp_path):
    path=tmp_path/'source.docx';fixture_docx(path);original=path.read_bytes()
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('source.docx')
        index=plugin.list_objects(path='source.docx',revision=revision)
        operation={'id':'e1','op':'replace_text','target':'paragraph:1','expected_hash':index['objects'][0]['hash'],
                   'start':1,'end':6,'text':'New'}
        args=dict(path='source.docx',revision=revision,operations=[operation],idempotency_key='word-one')
        receipt=plugin.stage_batch(**args)
        assert path.read_bytes()==original
        assert receipt==plugin.stage_batch(**args)
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply')
        edited=path.read_bytes();assert edited!=original
        undo=store.undo(receipt['receipt_id'],'word-undo')
        assert path.read_bytes()==edited
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert path.read_bytes()==original
    finally:store.close()


def test_word_external_change_blocks_apply(tmp_path):
    path=tmp_path/'source.docx';fixture_docx(path)
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('source.docx'); obj=plugin.list_objects(path='source.docx',revision=revision)['objects'][0]
        receipt=plugin.stage_batch(path='source.docx',revision=revision,idempotency_key='edit',operations=[
            {'id':'e1','op':'set_style','target':obj['id'],'expected_hash':obj['hash'],'style_id':'Heading1'}])
        path.write_bytes(b'external edit')
        with pytest.raises(HarnessError,match='outside'):
            store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply')
        assert path.read_bytes()==b'external edit'
    finally:store.close()


def test_binary_commit_recovery_never_rewrites_candidate(tmp_path):
    path=tmp_path/'source.docx';fixture_docx(path)
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    revision=store.file_hash('source.docx'); obj=plugin.list_objects(path='source.docx',revision=revision)['objects'][0]
    receipt=plugin.stage_batch(path='source.docx',revision=revision,idempotency_key='edit',operations=[
        {'id':'e1','op':'set_style','target':obj['id'],'expected_hash':obj['hash'],'style_id':'Heading1'}])
    def fault(point):
        if point=='after_replace':raise RuntimeError('Simulated receipt persistence interruption')
    with pytest.raises(RuntimeError):
        store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply',fault=fault)
    data=path.read_bytes();mtime=path.stat().st_mtime_ns;store.close()
    reopened=ProjectStore(tmp_path)
    try:
        assert reopened.operation(receipt['receipt_id'])['status']=='committed'
        reopened.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply')
        assert path.read_bytes()==data and path.stat().st_mtime_ns==mtime
        assert reopened.db.execute("SELECT count(*) FROM events WHERE operation_id=? AND state='committed'",(receipt['receipt_id'],)).fetchone()[0]==1
    finally:reopened.close()


def test_repeated_satisfied_edit_does_not_stage_again(tmp_path):
    path=tmp_path/'source.docx';fixture_docx(path)
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('source.docx'); obj=plugin.list_objects(path='source.docx',revision=revision)['objects'][0]
        op={'id':'e1','op':'replace_text','target':obj['id'],'expected_hash':obj['hash'],'start':0,'end':1,'text':'Z'}
        first=plugin.stage_batch(path='source.docx',revision=revision,idempotency_key='first',operations=[op])
        op['expected_hash']=first['results'][0]['after_hash']
        again=plugin.stage_batch(path='source.docx',revision=first['candidate_revision'],idempotency_key='repeat',operations=[op])
        assert again['status']=='unchanged' and again['receipt_id']==first['receipt_id']
        assert again['candidate_revision']==first['candidate_revision']
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
    finally:store.close()
