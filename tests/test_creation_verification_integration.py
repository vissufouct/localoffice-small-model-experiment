import json
from localoffice.storage import ProjectStore,canonical
from localoffice.word.plugin import WordPlugin
from localoffice.word.progress import completed_changes
from test_word_progress import task


def test_actual_creation_evidence_is_durable_revision_bound_and_bounded(tmp_path):
    store=ProjectStore(tmp_path);chat=task(store)
    try:
        request={'path':'report.docx','blocks':[{'kind':'paragraph','text':'First page'},{'kind':'page_break'},{'kind':'paragraph','text':'Second page'}],
                 'footer':{'text':'Page of','page_numbers':'current_total','separator':' of ','alignment':'left'}}
        result=WordPlugin(store).create(**request,idempotency_key='verify',task_id='task')
        assert len(canonical(result['creation_verification']).encode())<=1100
        event=json.loads(store.db.execute("SELECT detail FROM events WHERE operation_id=? AND state='validated'",(result['receipt_id'],)).fetchone()[0])
        assert 'creation_evidence' in event
        before=completed_changes(store,'task',chat,'report.docx',result['candidate_revision'])
        assert 'creation_verification' in before['receipts'][0]
        assert before['receipts'][0]['matches_current_revision']
        assert len(canonical(before).encode())<=2000
        store.close();store=ProjectStore(tmp_path)
        assert completed_changes(store,'task',chat,'report.docx',result['candidate_revision'])==before
        assert WordPlugin(store).create(**request,idempotency_key='verify',task_id='task')==result
        stale=completed_changes(store,'task',chat,'report.docx','0'*64)
        assert not stale['receipts'][0]['matches_current_revision']
    finally:store.close()


def test_creation_verification_receipt_contract_is_bounded_and_qualified():
    import pytest
    from pydantic import ValidationError
    from localoffice.word.contracts import BatchReceipt
    base={'receipt_id':'receipt','input_revision':None,'candidate_revision':'0'*64}
    small={'scope':'XML only','omitted':True,'counts':{'paragraph':2}}
    assert BatchReceipt(**base,creation_verification=small).creation_verification==small
    for value in ({'scope':'XML only','omitted':False}, {'scope':'XML only','omitted':True,'claimed_complete':True}, {'scope':'界'*1100,'omitted':True}):
        with pytest.raises(ValidationError):BatchReceipt(**base,creation_verification=value)
