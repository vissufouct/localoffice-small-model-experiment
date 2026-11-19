import pytest
from localoffice.storage import ProjectStore


def test_outer_failure_rolls_back_successful_nested_writes(tmp_path):
    store=ProjectStore(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.db.execute("INSERT INTO meta VALUES('outer-test','one')")
                with store.transaction():
                    store.db.execute("INSERT INTO meta VALUES('inner-test','two')")
                raise RuntimeError('outer interruption')
        assert store.db.execute("SELECT count(*) FROM meta WHERE key IN ('outer-test','inner-test')").fetchone()[0]==0
        assert not store.db.in_transaction
    finally:store.close()


def test_caught_inner_failure_preserves_outer_work_and_commit(tmp_path):
    store=ProjectStore(tmp_path)
    try:
        with store.transaction():
            store.db.execute("INSERT INTO meta VALUES('outer-test','one')")
            with pytest.raises(RuntimeError):
                with store.transaction():
                    store.db.execute("INSERT INTO meta VALUES('inner-test','two')")
                    raise RuntimeError('inner interruption')
        assert not store.db.in_transaction
    finally:store.close()
    store=ProjectStore(tmp_path)
    try:
        assert store.db.execute("SELECT value FROM meta WHERE key='outer-test'").fetchone()[0]=='one'
        assert store.db.execute("SELECT value FROM meta WHERE key='inner-test'").fetchone() is None
    finally:store.close()


def test_outer_scope_commits_pending_legacy_write_on_restart(tmp_path):
    store=ProjectStore(tmp_path)
    store.db.execute("INSERT INTO meta VALUES('legacy-test','pending')")
    with store.transaction():
        store.message('user','persist both')
    assert not store.db.in_transaction
    store.close();store=ProjectStore(tmp_path)
    try:
        assert store.db.execute("SELECT value FROM meta WHERE key='legacy-test'").fetchone()[0]=='pending'
        assert store.db.execute("SELECT content FROM messages WHERE role='user'").fetchone()[0]=='persist both'
    finally:store.close()
