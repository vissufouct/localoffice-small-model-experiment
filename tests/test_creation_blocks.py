import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.storage import canonical,digest
from localoffice.native_history import record_call,record_result
from localoffice.word import creation_blocks as blocks,creation_repair as repair
from localoffice.word.creation_revision import revise
from localoffice.word.contracts import EditCreationBlocks
from test_creation_revision import context,snapshot


def test_sparse_false_and_preservation(context):
    store,payload,ref,_=context
    original=copy.deepcopy(payload)
    changes=[{'op':'set_paragraph','index':0,'page_break_before':False,'text':''}]
    receipt=blocks.edit(store,'task',ref=ref,changes=changes)
    expected=copy.deepcopy(original);expected['blocks'][0].update(text='',page_break_before=False)
    state=repair.get(store,'task')
    assert state['payload']==expected and payload==original
    assert state['block_revision_origin']=={'ref':ref,'request_hash':digest(canonical(blocks.normalize({'ref':ref,'changes':changes})).encode())}
    assert blocks.edit(store,'task',ref=ref,changes=changes)==receipt
    assert store.candidate('report.docx') is None
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_edit_replay_and_generic_supersession(context):
    store,_,ref,_=context
    changes=[{'op':'remove_block','index':1},{'op':'set_paragraph','index':0,'style':'Normal'}]
    receipt=blocks.edit(store,'task',ref=ref,changes=changes)
    assert repair.get(store,'task')['payload']['blocks']==[{'kind':'paragraph','text':'Original','style':'Normal'}]
    with pytest.raises(HarnessError):blocks.edit(store,'task',ref=ref,changes=[{'op':'remove_block','index':0}])
    # A generic no-op revision must supersede the typed replay identity too.
    revise(store,'task',receipt['ref'],[{'op':'set','path':'blocks.0.style','value':'Normal'}])
    with pytest.raises(HarnessError):blocks.edit(store,'task',ref=ref,changes=changes)


@pytest.mark.parametrize('change',[
 {'op':'set_paragraph','index':1,'text':'Not table'},
 {'op':'remove_block','index':3},
 {'op':'set_paragraph','index':0,'text':{'ref':'ct_'+'0'*24}},
])
def test_atomic_failure(context,change):
    store,_,ref,_=context;before=snapshot(store)
    with pytest.raises((HarnessError,ValidationError)):
        blocks.edit(store,'task',ref=ref,changes=[{'op':'set_paragraph','index':0,'text':'Interim'},change])
    assert snapshot(store)==before


@pytest.mark.parametrize('change',[
 {'op':'set_paragraph','index':0}, {'op':'set_paragraph','index':0,'text':None},
 {'op':'set_paragraph','index':0,'text':False}, {'op':'set_paragraph','index':0,'style':False},
 {'op':'set_paragraph','index':0,'page_break_before':0},
 {'op':'set_paragraph','index':True,'text':'X'}, {'op':'remove_block','index':-1},
 {'op':'remove_block','index':24}, {'op':'remove_block','index':0,'value':False},
 {'op':'set_paragraph','index':0,'text':'x'*8001},
])
def test_strict_contract(change):
    with pytest.raises(ValidationError):EditCreationBlocks(ref='cr_'+'a'*32,changes=[change])


@pytest.mark.parametrize('count',[0,5])
def test_change_bounds(count):
    with pytest.raises(ValidationError):EditCreationBlocks(ref='cr_'+'a'*32,changes=[{'op':'remove_block','index':0}]*count)


def test_native_occurrence_and_historical_receipts(context):
    store,_,ref,_=context;request={'ref':ref,'changes':[{'op':'remove_block','index':1}]}
    action={'tool':'word.edit_creation_blocks',**request}
    first=record_call(store,'task',action)
    receipt=blocks.edit(store,'task',**request,native_call_id=first)
    record_result(store,'task',first,receipt)
    second=record_call(store,'task',action)
    assert blocks.result(store,'task',request,native_call_id=second) is None
    assert blocks.edit(store,'task',**request,native_call_id=second)==receipt
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    before=snapshot(store)
    assert blocks.result(store,'task',request,native_call_id=first)==receipt
    with pytest.raises(HarnessError):blocks.edit(store,'task',**request)
    assert snapshot(store)==before


@pytest.mark.parametrize('part',['receipt','request','envelope'])
def test_receipt_tamper(context,part):
    store,_,ref,_=context;request={'ref':ref,'changes':[{'op':'remove_block','index':1}]}
    call=record_call(store,'task',{'tool':'word.edit_creation_blocks',**request})
    blocks.edit(store,'task',**request,native_call_id=call)
    key=blocks.PREFIX+call
    envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
    if part=='request':envelope['state']['request']['changes'][0]['index']=0
    else:envelope['state']['receipt']['status']='invented'
    if part!='envelope':envelope['hash']=digest(canonical(envelope['state']).encode())
    with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),key))
    with pytest.raises(HarnessError,match='integrity'):blocks.result(store,'task',request,native_call_id=call)


def test_native_wrong_arguments_and_owner(context):
    from localoffice.chats import create_chat,select_chat
    store,_,ref,_=context;request={'ref':ref,'changes':[{'op':'remove_block','index':1}]}
    call=record_call(store,'task',{'tool':'word.edit_creation_blocks',**request})
    with pytest.raises(HarnessError):blocks.edit(store,'task',ref=ref,changes=[{'op':'remove_block','index':0}],native_call_id=call)
    select_chat(store,create_chat(store))
    with pytest.raises(HarnessError):blocks.result(store,'task',request,native_call_id=call)


def test_last_block_removal_atomic(context):
    store,_,ref,_=context;before=snapshot(store)
    with pytest.raises((HarnessError,ValidationError)):
        blocks.edit(store,'task',ref=ref,changes=[{'op':'remove_block','index':1},{'op':'remove_block','index':0}])
    assert snapshot(store)==before


def test_inline_source_and_break_correction(context):
    from localoffice.word.content_plan import plan
    store,_,_,_=context
    source=plan(store,'task',literals=[{'role':'paragraph','quote':'Exact source'}])['literals'][0]['ref']
    receipt=repair.capture(store,'task',{'path':'report.docx','blocks':[
        {'kind':'paragraph','text':'Keep'},{'kind':'page_break'}]},allow_valid=True)
    assert receipt['status']=='invalid_design'
    result=blocks.edit(store,'task',ref=receipt['ref'],changes=[
        {'op':'remove_block','index':1},
        {'op':'set_paragraph','index':0,'text':{'ref':source}}])
    assert result['status']=='prepared'
    assert repair.get(store,'task')['payload']['blocks']==[{'kind':'paragraph','text':{'ref':source}}]


def test_native_receipt_failure_rolls_back(context):
    store,_,ref,_=context;request={'ref':ref,'changes':[{'op':'remove_block','index':1}]}
    call=record_call(store,'task',{'tool':'word.edit_creation_blocks',**request})
    with store.transaction():
        store.db.execute("CREATE TRIGGER reject_blocks BEFORE INSERT ON meta WHEN NEW.key LIKE 'word_creation_block_result:%' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    before=snapshot(store)
    with pytest.raises(Exception,match='interrupted'):blocks.edit(store,'task',**request,native_call_id=call)
    assert snapshot(store)==before


@pytest.mark.parametrize('op',['remove_block','set_paragraph'])
@pytest.mark.parametrize('index',[True,False,'0','1',0.0,1.0])
@pytest.mark.parametrize('transport',['python','json'])
def test_indices_reject_coercion(op,index,transport):
    change={'op':op,'index':index}
    if op=='set_paragraph':change['text']='Keep'
    request={'ref':'cr_'+'a'*32,'changes':[change]}
    with pytest.raises(ValidationError):
        if transport=='json':EditCreationBlocks.model_validate_json(json.dumps(request))
        else:EditCreationBlocks.model_validate(request)


@pytest.mark.parametrize('value',[0,1,'true','false','0','1'])
@pytest.mark.parametrize('transport',['python','json'])
def test_page_break_rejects_coercion(value,transport):
    request={'ref':'cr_'+'a'*32,'changes':[
        {'op':'set_paragraph','index':0,'page_break_before':value}]}
    with pytest.raises(ValidationError):
        if transport=='json':EditCreationBlocks.model_validate_json(json.dumps(request))
        else:EditCreationBlocks.model_validate(request)


@pytest.mark.parametrize('value',[True,False])
def test_strict_page_break_and_omission_roundtrip(value):
    request=EditCreationBlocks(ref='cr_'+'a'*32,changes=[
        {'op':'remove_block','index':23},
        {'op':'set_paragraph','index':0,'page_break_before':value},
        {'op':'set_paragraph','index':1,'text':'Keep'}])
    normalized=EditCreationBlocks.model_validate_json(request.model_dump_json()).model_dump()
    assert normalized['changes'][1]=={'op':'set_paragraph','index':0,'page_break_before':value}
    assert normalized['changes'][2]=={'op':'set_paragraph','index':1,'text':'Keep'}


@pytest.mark.parametrize('reverse',[False,True])
def test_original_indices_sets_before_descending_removals(context,reverse):
    store,_,_,_=context
    payload={'path':'report.docx','blocks':[
        {'kind':'paragraph','text':'First'}, {'kind':'paragraph','text':'Remove'},
        {'kind':'paragraph','text':'Third'}, {'kind':'page_break'}]}
    ref=repair.capture(store,'task',payload,allow_valid=True)['ref']
    changes=[{'op':'remove_block','index':1},
             {'op':'set_paragraph','index':2,'page_break_before':True},
             {'op':'remove_block','index':3},
             {'op':'set_paragraph','index':2,'text':'Edited third'}]
    if reverse:changes.reverse()
    original=copy.deepcopy(changes)
    receipt=blocks.edit(store,'task',ref=ref,changes=changes)
    assert receipt['status']=='prepared'
    assert repair.get(store,'task')['payload']['blocks']==[
        {'kind':'paragraph','text':'First'},
        {'kind':'paragraph','text':'Edited third','page_break_before':True}]
    assert changes==original
    assert blocks.edit(store,'task',ref=ref,changes=changes)==receipt


@pytest.mark.parametrize('changes',[
    [{'op':'remove_block','index':0},{'op':'set_paragraph','index':0,'text':'Conflict'}],
    [{'op':'set_paragraph','index':0,'text':'Conflict'},{'op':'remove_block','index':0}],
    [{'op':'remove_block','index':1},{'op':'remove_block','index':1}],
    [{'op':'set_paragraph','index':0,'text':'Same'},{'op':'set_paragraph','index':0,'text':'Same'}],
    [{'op':'set_paragraph','index':0,'page_break_before':False},{'op':'set_paragraph','index':0,'page_break_before':True}],
])
def test_snapshot_conflicts_fail_atomically(context,changes):
    store,_,ref,_=context;before=snapshot(store)
    with pytest.raises(HarnessError):blocks.edit(store,'task',ref=ref,changes=changes)
    assert snapshot(store)==before


def test_original_kind_validated_before_any_removal(context):
    store,_,_,_=context
    ref=repair.capture(store,'task',{'path':'report.docx','blocks':[
        {'kind':'paragraph','text':'First'},{'kind':'page_break'},
        {'kind':'paragraph','text':'Third'}]},allow_valid=True)['ref']
    before=snapshot(store)
    with pytest.raises(HarnessError,match='paragraph in the referenced proposal'):
        blocks.edit(store,'task',ref=ref,changes=[
            {'op':'remove_block','index':0},{'op':'set_paragraph','index':1,'text':'No'}])
    assert snapshot(store)==before
