"""PDF/SQLite fixtures only; no office renderer or inference."""
import copy
import io
import json
import pytest
from pypdf import PdfWriter
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.errors import HarnessError
from localoffice.chats import current_chat,create_chat,select_chat
from localoffice.word.render_context import remember,summary,PREFIX,MAX_RECORD_BYTES


@pytest.fixture
def context(tmp_path):
    store=ProjectStore(tmp_path)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('task','Preview','running','live','report.docx',1,1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)',('task',current_chat(store)))
    (tmp_path/'report.docx').write_bytes(b'DOCX revision fixture, not parsed')
    writer=PdfWriter()
    for _ in range(2):writer.add_blank_page(width=100,height=100)
    buffer=io.BytesIO();writer.write(buffer)
    result={'path':'report.docx','revision':store.file_hash('report.docx'),
        'pdf_hash':store.put_blob(buffer.getvalue()),'pages':2,'cached':False,
        'configuration':{'renderer':'LibreOffice 26.2.2.2 build-id','filter':'writer_pdf_Export','fonts':'a'*64,'adapter_version':5},
        'warnings':['Font substitution is not diagnosed.']}
    yield store,result
    store.close()


def raw(store):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+'task',)).fetchone()
    return row[0] if row else None


def mutate(store,change,rehash=False):
    envelope=json.loads(raw(store));change(envelope)
    if rehash:envelope['hash']=digest(canonical(envelope['state']).encode())
    with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),PREFIX+'task'))


def test_durable_receipt_full_provenance_and_no_input_changes(context):
    store,result=context;before=copy.deepcopy(result)
    assert remember(store,'task',result) is result and result==before
    stored=raw(store)
    assert len(stored.encode())<=MAX_RECORD_BYTES
    assert json.loads(stored)['state']['result']==result
    projection=summary(store,'task',result['path'],result['revision'])
    assert projection['pages']==2 and projection['revision']==result['revision']
    assert projection['preview_does_not_update_docx_caches'] is True
    assert projection['visual_approval'] is False
    assert 'pdf_hash' not in projection and len(canonical(projection).encode())<=300
    assert remember(store,'task',result) is result and raw(store)==stored
    root=store.root;store.close();reopened=ProjectStore(root)
    try:assert summary(reopened,'task',result['path'],result['revision'])==projection
    finally:reopened.close()


def test_no_arbitrary_cache_or_missing_revision_facts(context):
    store,result=context
    with store.transaction():store.db.execute('INSERT INTO render_cache VALUES(?,?,?)',('cache',result['revision'],canonical(result)))
    assert summary(store,'task',result['path'],result['revision']) is None
    remember(store,'task',result)
    assert summary(store,'task',result['path'],None) is None
    assert summary(store,None,result['path'],result['revision']) is None
    assert remember(store,None,result) is result


@pytest.mark.parametrize('change',['revision','path','target','missing_target','changed_file'])
def test_summary_omits_stale_or_noncurrent_target(context,change):
    store,result=context;remember(store,'task',result)
    path=result['path'];revision=result['revision']
    if change=='revision':revision='0'*64
    elif change=='path':path='other.docx'
    elif change in ('target','missing_target'):
        with store.transaction():store.db.execute("UPDATE tasks SET artifact=? WHERE id='task'",('other.docx' if change=='target' else None,))
    else:(store.root/'report.docx').write_bytes(b'changed')
    assert summary(store,'task',path,revision) is None


def test_current_candidate_precedes_saved_file(context):
    store,result=context
    candidate=store.put_blob(b'candidate bytes');result['revision']=candidate
    with store.transaction():store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',('report.docx','op',store.file_hash('report.docx'),candidate))
    remember(store,'task',result)
    assert summary(store,'task','report.docx',candidate)['pages']==2
    with store.transaction():store.db.execute('DELETE FROM candidates')
    assert summary(store,'task','report.docx',candidate) is None


@pytest.mark.parametrize('mode',['chat','task','target','stale'])
def test_remember_checks_ownership_target_and_revision(context,mode):
    store,result=context
    if mode=='chat':select_chat(store,create_chat(store))
    elif mode=='target':result['path']='other.docx'
    elif mode=='stale':result['revision']='0'*64
    with pytest.raises(HarnessError):remember(store,'unknown' if mode=='task' else 'task',result)
    assert raw(store) is None


@pytest.mark.parametrize('key,value',[('pages',True),('pages',0),('pages',2001),('pages',3),
    ('cached',1),('pdf_hash','bad'),('configuration',{}),('warnings','warning'),('surprise',True)])
def test_invalid_receipt_cannot_replace_existing_record(context,key,value):
    store,result=context;remember(store,'task',result);before=raw(store)
    bad=copy.deepcopy(result);bad[key]=value
    with pytest.raises(HarnessError):remember(store,'task',bad)
    assert raw(store)==before


@pytest.mark.parametrize('mutation',[
    lambda e:e['state']['result'].update(pages=1),
    lambda e:e['state'].update(task_id='other'),
    lambda e:e['state']['result'].update(pages=True),
    lambda e:e['state']['result'].update(path='../outside.docx'),
])
def test_corruption_fails_even_on_stale_summary_request(context,mutation):
    store,result=context;remember(store,'task',result)
    mutate(store,mutation,rehash=True)
    # The valid changed page count needs an invalid envelope to signal corruption.
    if json.loads(raw(store))['state']['result']['pages']==1 and type(json.loads(raw(store))['state']['result']['pages']) is int:
        mutate(store,lambda e:e.update(hash='0'*64))
    with pytest.raises(HarnessError):summary(store,'task','report.docx',None)
    with pytest.raises(HarnessError):remember(store,'task',result)


@pytest.mark.parametrize('mode',['delete','alter','invalid_pdf'])
def test_pdf_provenance_integrity(context,mode):
    store,result=context
    if mode=='invalid_pdf':
        result['pdf_hash']=store.put_blob(b'not a PDF')
        with pytest.raises(HarnessError):remember(store,'task',result)
        return
    remember(store,'task',result)
    blob=store.blobs/result['pdf_hash']
    if mode=='delete':blob.unlink()
    else:blob.write_bytes(b'tampered')
    with pytest.raises(HarnessError):summary(store,'task','report.docx',result['revision'])


def test_summary_does_not_parse_pdf_again(context,monkeypatch):
    store,result=context;remember(store,'task',result)
    def forbidden(*args,**kwargs):raise AssertionError('No prompt-time PDF parsing')
    monkeypatch.setattr('pypdf.PdfReader',forbidden)
    assert summary(store,'task','report.docx',result['revision'])['pages']==2


def test_field_snapshot_does_not_claim_target_caches_updated(context):
    store,result=context
    result['field_snapshot_hash']=store.put_blob(b'private refreshed snapshot')
    result['field_update']={'update_passes':2,'indexes_updated':0}
    remember(store,'task',result)
    assert json.loads(raw(store))['state']['result']==result
    assert summary(store,'task','report.docx',result['revision'])['preview_does_not_update_docx_caches'] is True
    (store.blobs/result['field_snapshot_hash']).unlink()
    with pytest.raises(HarnessError):summary(store,'task','report.docx',result['revision'])


def test_unicode_renderer_abbreviation_and_bounded_provenance(context):
    store,result=context;result['configuration']['renderer']='界😀'*100
    remember(store,'task',result)
    projection=summary(store,'task','report.docx',result['revision'])
    assert projection['renderer_abbreviated'] is True
    assert result['configuration']['renderer'].startswith(projection['renderer'])
    assert len(canonical(projection).encode())<=300
    before=raw(store);bad=copy.deepcopy(result);bad['warnings']=['界'*1000]*10
    with pytest.raises(HarnessError):remember(store,'task',bad)
    assert raw(store)==before


def test_wrong_chat_cannot_read_existing_render_summary(context):
    store,result=context;remember(store,'task',result);select_chat(store,create_chat(store))
    with pytest.raises(HarnessError):summary(store,'task','report.docx',result['revision'])
