"""Range provenance preserves quote history and exact native call identity."""
import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.errors import HarnessError
from localoffice.native_history import record_call,record_result
from localoffice.word import requirements as req
from test_word_progress import task

TEXT='Use one-inch margins. Draft a sentence with "café" and a backslash \\. Keep exact Unicode 😀. End.'

@pytest.fixture
def ctx(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(TEXT,'task'))
    yield store,digest(TEXT.encode())
    store.close()

def requirement(**source):
    return dict(kind='page_geometry',field='margin',equals=1,unit='in',**source)

def ranged(store):
    from localoffice.word.objective_evidence import catalogue
    cat=catalogue(store,'task');assert cat['available']
    keys=list(cat['segments'])
    return requirement(source_range={'start':keys[0],'end':keys[-1]})

def snapshot(store):return [tuple(row) for row in store.db.execute('SELECT key,value FROM meta ORDER BY key')]

def test_legacy_normalization_and_v1_state_unchanged(ctx):
    store,h=ctx;entry=requirement(source_quote='one-inch margins');entry['equals']=1.0
    request={'tool':'word.plan_requirements','source_hash':h,'requirements':[entry]}
    normalized=req.normalize(request)
    assert canonical(normalized)==canonical(request)
    assert req.PlanRequirements.model_validate(request).model_dump()==request
    receipt=req.plan(store,'task',source_hash=h,requirements=[entry])
    state=req.get(store,'task');assert state['version']==1 and 'source_catalogue' not in state
    assert state['request_hash']==digest(canonical(request).encode())
    before=snapshot(store)
    assert req.get(store,'task')==state and snapshot(store)==before
    assert receipt['coverage']=='unassessed'


def test_full_contiguous_range_exact_source_and_original_request(ctx):
    store,h=ctx;entry=ranged(store);original=copy.deepcopy(entry)
    req.plan(store,'task',source_hash=h,requirements=[entry])
    state=req.get(store,'task')
    assert state['version']==2 and state['source_catalogue']['version']==1
    assert state['source_spans']==[{'start':0,'end':len(TEXT)}]
    assert state['source_text'][0:len(TEXT)]==TEXT
    assert state['request']['requirements']==[original] and entry==original
    assert 'source_quote' not in state['request']['requirements'][0]
    assert req.PlanRequirements.model_validate(state['request']).model_dump()==state['request']


@pytest.mark.parametrize('source',[{}, {'source_quote':'text','source_range':{'start':'s01','end':'s01'}},
    {'source_range':{'start':'s1','end':'s01'}}, {'source_range':{'start':'s01'}},
    {'source_range':{'start':'s00','end':'s25'}}, {'source_quote':''}])
def test_evidence_xor_and_closed_range(source):
    with pytest.raises(ValidationError):req.PlanRequirements(source_hash='a'*64,requirements=[requirement(**source)])


def test_mixed_generation_and_quote_range_call_identity(ctx):
    store,h=ctx;entry=ranged(store)
    args={'source_hash':h,'requirements':[entry]}
    call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
    receipt=req.plan(store,'task',**args,native_call_id=call)
    quoted={'source_hash':h,'requirements':[requirement(source_quote=TEXT)]}
    with pytest.raises(HarnessError):req.result(store,'task',quoted,native_call_id=call)
    record_result(store,'task',call,receipt)
    unexecuted=record_call(store,'task',{'tool':'word.plan_requirements',**args})
    assert req.result(store,'task',args,native_call_id=unexecuted) is None
    # Direct clear is a new immutable generation, not a rewrite of range history.
    req.plan(store,'task',source_hash=h,requirements=[])
    with store.transaction():store.db.execute("UPDATE tasks SET objective='New source' WHERE id='task'")
    assert req.result(store,'task',args,native_call_id=call)==receipt
    assert req.plan(store,'task',**args,native_call_id=call)==receipt
    assert req.get(store,'task')['version']==1


@pytest.mark.parametrize('mode',['reverse','missing','unavailable','stale'])
def test_bad_range_atomic(ctx,mode):
    store,h=ctx;entry=ranged(store)
    if mode=='reverse':entry['source_range']={k:v for k,v in zip(('start','end'),reversed(list(entry['source_range'].values())))}
    if mode=='missing':entry['source_range']={'start':'s24','end':'s24'}
    if mode=='unavailable':
        text='x'*3000
        with store.transaction():store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(text,'task'))
        h=digest(text.encode())
    if mode=='stale':h='0'*64
    before=snapshot(store)
    with pytest.raises(HarnessError):req.plan(store,'task',source_hash=h,requirements=[entry])
    assert snapshot(store)==before


@pytest.mark.parametrize('mode',['span','id','version','catalogue'])
def test_rehashed_range_state_tamper_rejected(ctx,mode):
    store,h=ctx;req.plan(store,'task',source_hash=h,requirements=[ranged(store)])
    state=req.get(store,'task')
    if mode=='span':state['source_spans'][0]['start']+=1
    elif mode=='id':
        state['request']['requirements'][0]['source_range']['start']='s24'
        state['request_hash']=req._hash(state['request'])
    elif mode=='version':state['source_catalogue']['version']=999
    else:state['source_catalogue']['segments']['s01']['text']='Changed'
    with store.transaction():
        store.db.execute('DELETE FROM meta WHERE key IN (?,?)',(req.HISTORY_PREFIX+'task:1',req.PREFIX+'task'))
        req._write(store,req.HISTORY_PREFIX+'task:1',state)
        req._write(store,req.PREFIX+'task',{'generation':1,'plan_hash':req._hash(state)})
    with pytest.raises(HarnessError):req.get(store,'task')


def test_ranges_do_not_certify_numeric_interpretation(ctx):
    store,h=ctx;entry=ranged(store);entry['equals']=2
    req.plan(store,'task',source_hash=h,requirements=[entry])
    assert req.get(store,'task')['request']['requirements'][0]['equals']==2
    from localoffice.word.requirement_checks import check
    from localoffice.word.contracts import CreateDocument
    result=check(store,'task',CreateDocument(path='report.docx',page={'margin_twips':2880},
        blocks=[{'kind':'paragraph','text':'Body'}]))
    assert result['status']=='all_selected_pass' and result['coverage']=='unassessed'
    assert result['interpretation']=='model_selected'


def test_wrong_owner_range_denied(ctx):
    from localoffice.chats import create_chat,select_chat
    store,h=ctx;entry=ranged(store);select_chat(store,create_chat(store))
    with pytest.raises(HarnessError):req.plan(store,'task',source_hash=h,requirements=[entry])


def test_legacy_native_receipt_remains_exact_after_source_change(ctx):
    store,h=ctx
    args={'source_hash':h,'requirements':[requirement(source_quote='one-inch margins')]}
    call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
    receipt=req.plan(store,'task',**args,native_call_id=call)
    assert req.get(store,'task')['version']==1
    before=snapshot(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    assert req.result(store,'task',args,native_call_id=call)==receipt
    assert req.plan(store,'task',**args,native_call_id=call)==receipt
    assert snapshot(store)==before


def test_mixed_quotes_and_ranges_keep_exact_spans(ctx):
    store,h=ctx;quote=requirement(source_quote='one-inch margins');selected=ranged(store)
    req.plan(store,'task',source_hash=h,requirements=[quote,selected])
    state=req.get(store,'task')
    assert state['version']==2
    spans=state['source_spans']
    assert TEXT[spans[0]['start']:spans[0]['end']]=='one-inch margins'
    assert TEXT[spans[1]['start']:spans[1]['end']]==TEXT
    assert 'source_range' not in state['request']['requirements'][0]
    assert 'source_quote' not in state['request']['requirements'][1]


def test_unavailable_catalogue_keeps_quote_fallback(ctx):
    store,_=ctx;text='x'*3000+' unique constraint'
    with store.transaction():store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(text,'task'))
    receipt=req.plan(store,'task',source_hash=digest(text.encode()),
                     requirements=[requirement(source_quote='unique constraint')])
    assert receipt['status']=='planned' and req.get(store,'task')['version']==1
