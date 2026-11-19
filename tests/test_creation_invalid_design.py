"""Invalid designs are retained for explicit review, never made executable."""
import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.word import creation_repair as repair
from localoffice.word.creation_revision import revise
from localoffice.word.content_plan import plan,bind_creation
from localoffice.word.contracts import CreateDocument,CreateDocumentShape
from localoffice.word.plugin import WordPlugin
from test_word_content_plan import fixture


def proposal():
    return {'path':'report.docx','blocks':[{'kind':'paragraph','text':'First page'},
            {'kind':'paragraph','text':'Draft invitation'},
            {'kind':'paragraph','text':'Second page'},{'kind':'page_break'}]}


def snapshot(store):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(repair.PREFIX+'task',)).fetchone()
    return row[0] if row else None


def test_retention_exact_restart_finalize_refusal_then_explicit_revision(tmp_path,monkeypatch):
    store=fixture(tmp_path);payload=proposal();original=copy.deepcopy(payload)
    monkeypatch.setattr(WordPlugin,'_execute',lambda *a,**kw:pytest.fail('No worker for invalid design'))
    try:
        summary=repair.capture(store,'task',payload,allow_valid=True)
        assert summary['status']=='invalid_design' and summary['document_changed'] is False
        errors=summary['validation_errors']
        assert len(canonical(errors).encode())<=640 and errors['items']
        assert 'between content blocks' in errors['items'][0]['message']
        state=repair.get(store,'task');assert state['payload']==original==payload
        assert state['payload_hash']==digest(canonical(original).encode())
        assert repair.capture(store,'task',payload,allow_valid=True)==summary
        before=snapshot(store)
        with pytest.raises(ValidationError):
            WordPlugin(store).finalize_creation(task_id='task',idempotency_key='bad',ref=summary['ref'],styles=[])
        assert snapshot(store)==before
        store.close();store=ProjectStore(tmp_path)
        assert repair.pending(store,'task')==summary
        changes=[{'op':'remove','path':'blocks.3'},
                 {'op':'set','path':'blocks.2.page_break_before','value':True}]
        revised=revise(store,'task',summary['ref'],changes)
        assert revised['status']=='prepared' and 'validation_errors' not in revised
        assert revise(store,'task',summary['ref'],changes)==revised
        request=repair.resolve(store,'task',revised['ref'],[])
        assert isinstance(request,CreateDocument) and request.blocks[2].page_break_before
        expected=copy.deepcopy(original);expected['blocks'].pop();expected['blocks'][2]['page_break_before']=True
        assert repair.get(store,'task')['payload']==expected
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert not (tmp_path/'report.docx').exists()
    finally:store.close()


@pytest.mark.parametrize('kind',['byte_limit','xml_body','xml_band','shape','bad_ref','wrong_role','resolved_byte_limit'])
def test_hard_guards_never_become_retained_design_errors(tmp_path,kind):
    store=fixture(tmp_path);payload=proposal()
    try:
        if kind=='byte_limit':payload['blocks']=[{'kind':'paragraph','text':'漢'*7000}]*2
        elif kind=='xml_body':payload['blocks'][0]['text']='bad\x01'
        elif kind=='xml_band':payload['footer']={'text':'bad\uffff'}
        elif kind=='shape':payload['blocks'][0]['unknown']=True
        elif kind=='bad_ref':payload['blocks'][0]['text']={'ref':'ct_'+'0'*24}
        else:
            if kind=='resolved_byte_limit':
                with store.transaction():store.db.execute("UPDATE tasks SET objective=? WHERE id='task'",('漢'*7000,))
                ref=plan(store,'task',literals=[{'role':'paragraph','quote':'漢'*7000}])['literals'][0]['ref']
                payload['blocks']=[{'kind':'paragraph','text':{'ref':ref}},
                                   {'kind':'paragraph','text':{'ref':ref}},{'kind':'page_break'}]
            else:
                ref=plan(store,'task',literals=[{'role':'footer','quote':'First page'}])['literals'][0]['ref']
                payload['blocks'][0]['text']={'ref':ref}
        with pytest.raises((ValidationError,HarnessError,ValueError)):
            repair.capture(store,'task',payload,allow_valid=True)
        assert snapshot(store) is None
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def test_invalid_revision_atomic_and_remaining_design_error_explicit(tmp_path):
    store=fixture(tmp_path)
    try:
        result=repair.capture(store,'task',proposal(),allow_valid=True);before=snapshot(store)
        with pytest.raises(ValidationError):
            revise(store,'task',result['ref'],[{'op':'set','path':'blocks.0.text','value':'bad\x00'}])
        assert snapshot(store)==before
        still=revise(store,'task',result['ref'],[{'op':'set','path':'blocks.1.text','value':'New draft'}])
        assert still['status']=='invalid_design' and still['ref']!=result['ref']
        with pytest.raises(ValidationError):repair.resolve(store,'task',still['ref'],[])
    finally:store.close()


def test_bound_source_provenance_preserved_and_stale_rejected(tmp_path):
    store=fixture(tmp_path)
    try:
        ref=plan(store,'task',literals=[{'role':'paragraph','quote':'First page'}])['literals'][0]['ref']
        payload=proposal();payload['blocks'][0]['text']={'ref':ref}
        summary=repair.capture(store,'task',payload,allow_valid=True)
        assert repair.get(store,'task')['literal_bindings'][0]['ref']==ref
        with pytest.raises(ValidationError):bind_creation(store,'task',CreateDocumentShape(**payload))
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        assert repair.pending(store,'task')['status']=='stale'
        with pytest.raises(HarnessError):revise(store,'task',summary['ref'],[{'op':'remove','path':'blocks.3'}])
    finally:store.close()


def test_nonreview_abstains_and_tamper_fails_closed(tmp_path):
    store=fixture(tmp_path)
    try:
        payload=proposal();payload['header']={'text':'Heading','style':'Missing'}
        assert repair.capture(store,'task',payload) is None
        summary=repair.capture(store,'task',payload,allow_valid=True)
        assert summary['status']=='invalid_design' and summary['missing_styles'][0]['id']=='Missing'
        envelope=json.loads(snapshot(store));envelope['state']['payload']['blocks'].pop()
        with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),repair.PREFIX+'task'))
        with pytest.raises(HarnessError) as caught:repair.pending(store,'task')
        assert caught.value.code=='state_corrupt'
    finally:store.close()


def test_nonreview_invalid_design_still_abstains_before_absent_target_check(tmp_path):
    store=fixture(tmp_path)
    try:
        payload=proposal();payload['header']={'text':'Heading','style':'Missing'}
        (tmp_path/'report.docx').write_bytes(b'existing')
        assert repair.capture(store,'task',payload) is None
        assert snapshot(store) is None
    finally:store.close()
