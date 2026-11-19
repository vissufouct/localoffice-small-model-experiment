import copy,json
import pytest
from localoffice.storage import ProjectStore,canonical
from localoffice.word.plugin import WordPlugin
from localoffice.word.progress import completed_changes
from localoffice.word.receipt_context import project
from localoffice.native_history import record_call,record_result
from test_word_progress import task


@pytest.fixture
def context(tmp_path):
    store=ProjectStore(tmp_path);chat=task(store)
    args={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Exact body'}]}
    call=record_call(store,'task',{'tool':'word.create',**args})
    receipt=WordPlugin(store).create(**args,task_id='task',idempotency_key='native:'+call)
    message=record_result(store,'task',call,receipt)
    progress=completed_changes(store,'task',chat,'report.docx',receipt['candidate_revision'])
    yield store,chat,call,message,receipt,progress
    store.close()


def projection(context,receipt=None,progress=None,message=None,revision=None):
    store,chat,call,mid,raw,state=context
    return project(store,'task',chat,mid if message is None else message,raw if receipt is None else receipt,
                   state if progress is None else progress,'report.docx',raw['candidate_revision'] if revision is None else revision)


def test_duplicate_facts_removed_only_from_prompt_and_unique_facts_remain(context):
    store,_,_,message,receipt,progress=context
    before=canonical(receipt);saved=store.db.execute('SELECT content FROM messages WHERE id=?',(message,)).fetchone()[0]
    partial=copy.deepcopy(progress);partial['receipts'][0]['creation_verification']['body']=[]
    result=projection(context,progress=partial)
    assert result['creation_verification']['body']==receipt['creation_verification']['body']
    assert result['creation_verification']['repeated_facts_in_current_state'] is True
    assert 'counts' not in result['creation_verification']
    assert result['creation_verification']['scope']==receipt['creation_verification']['scope']
    assert canonical(receipt)==before
    assert store.db.execute('SELECT content FROM messages WHERE id=?',(message,)).fetchone()[0]==saved
    assert len(canonical(result))<len(before)


@pytest.mark.parametrize('case',['wrong_message','wrong_revision','stale_progress','modified_receipt','unexecuted_call'])
def test_unproven_or_noncurrent_receipts_are_not_deduplicated(context,case):
    store,_,_,message,receipt,progress=context
    kwargs={}
    if case=='wrong_message':kwargs['message']=-1
    elif case=='wrong_revision':kwargs['revision']='0'*64
    elif case=='stale_progress':
        state=copy.deepcopy(progress);state['receipts'][0]['matches_current_revision']=False;kwargs['progress']=state
    elif case=='modified_receipt':
        raw=copy.deepcopy(receipt);raw['creation_verification']['body'][0]['text']='invented';kwargs['receipt']=raw
    else:
        call=record_call(store,'task',{'tool':'word.create','path':'report.docx','blocks':[{'kind':'paragraph','text':'Exact body'}]})
        kwargs['message']=record_result(store,'task',call,receipt)
    assert projection(context,**kwargs)==kwargs.get('receipt',receipt)
