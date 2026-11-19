import pytest
from localoffice.storage import ProjectStore
from localoffice.errors import HarnessError


def created(store):
    op=store.stage(path='new.md',text='# Created\n',expected_revision=None,idempotency_key='create')
    return store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')


def test_undo_creation_requires_explicit_remove_and_can_restore(tmp_path):
    store=ProjectStore(tmp_path);op=created(store)
    removal=store.undo(op['id'],'undo-create')
    assert (tmp_path/'new.md').read_text()=='# Created\n'
    assert store.snapshot()['candidates'][0]['removal']
    with pytest.raises(HarnessError):store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='apply')
    store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='remove')
    assert not (tmp_path/'new.md').exists()
    restore=store.undo(removal['id'],'restore')
    assert restore['linked_operation']==removal['id']
    store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
    assert (tmp_path/'new.md').read_text()=='# Created\n'
    store.close()


@pytest.mark.parametrize('boundary',['before_replace','after_replace'])
def test_removal_recovery_at_commit_boundaries(tmp_path,boundary):
    store=ProjectStore(tmp_path);op=created(store);removal=store.undo(op['id'],'undo')
    def crash(point):
        if point==boundary:raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='remove',fault=crash)
    store.close();store=ProjectStore(tmp_path)
    status=store.operation(removal['id'])['status']
    assert status==('validated' if boundary=='before_replace' else 'committed')
    if boundary=='before_replace':store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='remove')
    assert not (tmp_path/'new.md').exists()
    assert store.blob(removal['backup_hash'])==b'# Created\n'
    store.close()


def test_removal_preserves_external_edits(tmp_path):
    store=ProjectStore(tmp_path);op=created(store);removal=store.undo(op['id'],'undo')
    (tmp_path/'new.md').write_text('External revision')
    with pytest.raises(HarnessError) as error:store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='remove')
    assert error.value.code=='external_change'
    assert (tmp_path/'new.md').read_text()=='External revision'
    store.close()
