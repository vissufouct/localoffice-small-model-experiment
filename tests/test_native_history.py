import pytest
from localoffice.storage import ProjectStore
from localoffice.native_history import record_call, record_result, exchanges
from localoffice.native_tools import validate_history
from localoffice.chats import create_chat,select_chat
from localoffice.errors import HarnessError
from test_word_progress import task


def test_atomic_pairs_survive_restart_and_bounded_history(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    calls=[]
    for number in range(3):
        identity=record_call(store,'task',{'tool':'word.read_objects','path':'report.docx','ids':[str(number)]})
        calls.append(identity)
        record_result(store,'task',identity,{'objects':[]})
    store.close();store=ProjectStore(tmp_path)
    try:
        pairs=exchanges(store,'task',limit=2)
        assert len(pairs)==2
        assert [pair[0]['tool_calls'][0]['id'] for pair in pairs]==calls[1:]
        validate_history([m for pair in pairs for m in pair])
    finally:store.close()


def test_pending_call_does_not_silently_replay_and_duplicate_result_is_atomic(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    identity=record_call(store,'task',{'tool':'finish','text':'ok'})
    store.close();store=ProjectStore(tmp_path)
    try:
        for operation in (lambda:exchanges(store,'task'),lambda:record_call(store,'task',{'tool':'finish'})):
            with pytest.raises(HarnessError,match='pending|Pending'):operation()
        assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0]==1
        record_result(store,'task',identity,{'status':'recorded'})
        with pytest.raises(HarnessError):record_result(store,'task',identity,{'status':'different'})
        assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0]==2
    finally:store.close()


def test_wrong_task_and_chat_cannot_attach_result(tmp_path):
    store=ProjectStore(tmp_path);task(store);task(store,'other')
    try:
        identity=record_call(store,'task',{'tool':'read_file','path':'x'})
        with pytest.raises(HarnessError):record_result(store,'other',identity,{})
        select_chat(store,create_chat(store))
        with pytest.raises(HarnessError):record_result(store,'task',identity,{})
        with pytest.raises(HarnessError):exchanges(store,'task')
        assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0]==1
    finally:store.close()


def test_forget_clears_native_arguments_and_receipts(tmp_path):
    from localoffice.memory import forget_project_memory
    store=ProjectStore(tmp_path);task(store)
    try:
        identity=record_call(store,'task',{'tool':'read_file','path':'private.txt'})
        record_result(store,'task',identity,{'text':'private'})
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        assert store.db.execute('SELECT count(*) FROM native_calls').fetchone()[0]==0
    finally:store.close()


def test_result_and_recovery_record_rollback_together(tmp_path,monkeypatch):
    store=ProjectStore(tmp_path);task(store)
    try:
        identity=record_call(store,'task',{'tool':'read_file','path':'x'})
        original=store.message
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('interrupted before result linkage')
        monkeypatch.setattr(store,'message',interrupted)
        with pytest.raises(RuntimeError):
            record_result(store,'task',identity,{},recovery_state={'unresolved':False})
        assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0]==1
        assert store.db.execute('SELECT result_message FROM native_calls').fetchone()[0] is None
        assert store.db.execute("SELECT 1 FROM meta WHERE key='task_recovery:task'").fetchone() is None
    finally:store.close()
