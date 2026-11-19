import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from localoffice.storage import ProjectStore, digest
from localoffice.errors import HarnessError


@pytest.fixture
def store(tmp_path):
    s=ProjectStore(tmp_path)
    yield s
    s.close()


def staged(store, text='new', path='report.md', key='op1'):
    return store.stage(path=path,text=text,expected_revision=store.file_hash(path),idempotency_key=key)


def test_apply_undo_and_immutability(store):
    p=store.root/'report.md';p.write_text('original')
    op=staged(store)
    assert p.read_text()=='original'
    assert store.read('report.md')['text']=='new'
    store.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply')
    assert p.read_text()=='new'
    undo=store.undo(op['id'],'undo1')
    assert p.read_text()=='new'
    store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
    assert p.read_text()=='original'
    assert undo['linked_operation']==op['id']


def test_save_and_idempotent_retry(store):
    op=staged(store)
    assert not (store.root/'report.md').exists()
    same=staged(store)
    assert same['id']==op['id']
    with pytest.raises(HarnessError,match='different arguments'):staged(store,text='different')
    with pytest.raises(HarnessError,match='Save'):store.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply')
    store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    before=store.db.execute('SELECT count(*) FROM events').fetchone()[0]
    assert store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')['status']=='committed'
    assert store.db.execute('SELECT count(*) FROM events').fetchone()[0]==before


def test_conflict_does_not_overwrite(store):
    p=store.root/'report.md';p.write_text('old');op=staged(store)
    p.write_text('external edit')
    with pytest.raises(HarnessError,match='outside'):store.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply')
    assert p.read_text()=='external edit'


def test_stale_candidate(store):
    op=staged(store)
    with pytest.raises(HarnessError,match='Refresh'):staged(store,key='op2')
    store.stage(path='report.md',text='second',expected_revision=op['candidate_hash'],idempotency_key='op2')
    with pytest.raises(HarnessError,match='newer'):store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')


@pytest.mark.parametrize('path',['../outside.md','/tmp/outside.md','.localoffice/secrets.md'])
def test_scope(store,path):
    with pytest.raises(HarnessError):staged(store,path=path)


def test_symlink_denied(store,tmp_path):
    outside=tmp_path.parent/'outside.md';outside.write_text('safe')
    (store.root/'link.md').symlink_to(outside)
    with pytest.raises(HarnessError):staged(store,path='link.md')
    assert outside.read_text()=='safe'


@pytest.mark.parametrize('point,expected',[('before_replace','validated'),('after_replace','committed')])
def test_actual_process_crash(tmp_path,point,expected):
    (tmp_path/'report.md').write_text('old')
    s=ProjectStore(tmp_path);op=staged(s);s.close()
    code='''import os,sys
from localoffice.storage import ProjectStore
s=ProjectStore(sys.argv[1]);op=s.operation(sys.argv[2])
def fault(point):
 if point==sys.argv[3]:os._exit(77)
s.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply',fault=fault)
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'host')}
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path),op['id'],point],env=env)
    assert result.returncode==77
    s=ProjectStore(tmp_path)
    assert s.operation(op['id'])['status']==expected
    if expected=='committed':
        count=s.db.execute('SELECT count(*) FROM events').fetchone()[0]
        s.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply')
        assert s.db.execute('SELECT count(*) FROM events').fetchone()[0]==count
    else:
        assert (tmp_path/'report.md').read_text()=='old'
    s.close()


def test_recovery_never_guesses_external_content(store):
    p=store.root/'report.md';p.write_text('old');op=staged(store)
    with store.transaction():store.event(op['id'],'commit_pending')
    p.write_text('third party')
    store.recover()
    assert store.operation(op['id'])['status']=='recovery_required'
    assert p.read_text()=='third party'


def test_three_context_resets_keep_decisions_history_and_receipts(store):
    source=store.message('user','Use British spelling for this project.')
    store.decision('Use British spelling',source)
    op=staged(store)
    store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    for _ in range(3):
        state=store.checkpoint(summary_error='injected summarizer failure')
        assert state['decisions'][0]['content']=='Use British spelling'
        assert state['fallback']
        assert len(store.search('British'))==1
        assert store.operation(op['id'])['status']=='committed'
    assert store.db.execute('SELECT count(*) FROM checkpoints').fetchone()[0]==3


def test_project_isolation(store,tmp_path):
    other=tmp_path/'other';other.mkdir()
    second=ProjectStore(other)
    op=staged(store);store.message('user','private kiwi')
    assert second.id!=store.id
    assert second.search('kiwi')==[]
    with pytest.raises(HarnessError):second.operation(op['id'])
    second.close()


def test_corrupt_blob_rejected(store):
    op=staged(store)
    (store.blobs/op['candidate_hash']).write_text('tampered')
    with pytest.raises(HarnessError,match='hash check'):store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    assert not (store.root/'report.md').exists()


def test_exclusive_project_writer(tmp_path):
    first=ProjectStore(tmp_path)
    with pytest.raises(HarnessError,match='already open'):
        ProjectStore(tmp_path)
    first.close()
    reopened=ProjectStore(tmp_path)
    reopened.close()


@pytest.mark.parametrize('boundary',['before_rename','after_rename','receipt_transaction'])
def test_sigkill_inside_atomic_replace_and_receipt_transaction(tmp_path,boundary):
    """Parent kills the writer at a pipe-confirmed boundary, without cleanup."""
    import select
    import signal
    target=tmp_path/'report.md';target.write_text('old')
    store=ProjectStore(tmp_path);op=staged(store);store.close()
    child_code='''import os,signal,sys
import localoffice.storage as storage
store=storage.ProjectStore(sys.argv[1]);op=store.operation(sys.argv[2]);boundary=sys.argv[3]
def stop_here():
 print('boundary-reached',flush=True)
 while True:signal.pause()
original_replace=storage.os.replace
def replace(source,target):
 is_target=target==store.root/'report.md'
 if is_target and boundary=='before_rename':stop_here()
 original_replace(source,target)
 if is_target and boundary=='after_rename':stop_here()
storage.os.replace=replace
original_event=store.event
def event(operation,state,detail=None):
 original_event(operation,state,detail)
 if boundary=='receipt_transaction' and state=='committed':stop_here()
store.event=event
store.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply')
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'host')}
    child=subprocess.Popen([sys.executable,'-u','-c',child_code,str(tmp_path),op['id'],boundary],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    try:
        readable,_,_=select.select([child.stdout],[],[],10)
        assert readable,'Writer never reached crash boundary'
        assert child.stdout.readline()==b'boundary-reached\n'
        before_recovery=target.read_bytes();mtime=target.stat().st_mtime_ns
        child.kill();assert child.wait(timeout=5)==-signal.SIGKILL
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
        child.stdout.close();child.stderr.close()
    recovered=ProjectStore(tmp_path)
    try:
        assert target.read_bytes()==before_recovery
        assert target.stat().st_mtime_ns==mtime,'Recovery rewrote the original'
        if boundary=='before_rename':
            assert before_recovery==b'old'
            assert recovered.operation(op['id'])['status']=='validated'
            assert recovered.candidate('report.md') is not None
            assert recovered.db.execute("SELECT count(*) FROM events WHERE operation_id=? AND state='committed'",(op['id'],)).fetchone()[0]==0
        else:
            assert before_recovery==b'new'
            assert recovered.operation(op['id'])['status']=='committed'
            assert recovered.candidate('report.md') is None
            assert recovered.db.execute("SELECT count(*) FROM events WHERE operation_id=? AND state='committed'",(op['id'],)).fetchone()[0]==1
            event_count=recovered.db.execute('SELECT count(*) FROM events').fetchone()[0]
            recovered.apply(op['id'],expected_candidate=op['candidate_hash'],action='apply')
            assert recovered.db.execute('SELECT count(*) FROM events').fetchone()[0]==event_count
            recovered.close();recovered=ProjectStore(tmp_path)
            assert recovered.db.execute('SELECT count(*) FROM events').fetchone()[0]==event_count
            assert target.stat().st_mtime_ns==mtime
    finally:recovered.close()
