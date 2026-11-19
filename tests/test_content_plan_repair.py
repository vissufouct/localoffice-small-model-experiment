"""Core retained-selection repair: no models, workers, or rendering."""
import copy
import json

import pytest
from pydantic import ValidationError

from localoffice.chats import create_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.storage import canonical, digest, ProjectStore
from localoffice.word import content_plan as plans
from localoffice.word import content_plan_repair as repairs
from test_word_content_plan import fixture
from test_word_progress import task


def failed(store,choices=None,**kwargs):
    request={'literals':choices or [{'role':'header','quote':'Header'},
                                   {'role':'paragraph','quote':'A newly drafted sentence.'}],**kwargs}
    with pytest.raises(HarnessError) as error:plans.plan(store,'task',**request)
    summary=repairs.capture(store,'task',request,error.value)
    assert summary is not None
    return request,summary


def defer(summary,index=1):
    return {'ref':summary['ref'],'changes':[{'op':'defer_as_draft','index':index}]}


def database(store):
    return list(store.db.iterdump())


def test_capture_summary_preview_and_explicit_defer_preserve_original(tmp_path):
    store=fixture(tmp_path,'Header. Draft a sentence.')
    try:
        request,summary=failed(store);original=copy.deepcopy(request)
        assert summary['status']=='pending' and summary['selection_count']==2
        assert summary['invalid']==[{'index':1,'reason':'not_in_source','role':'paragraph',
                                     'preview':'A newly drafted sentence.','truncated':False}]
        assert not summary['truncated'] and summary['omitted_invalid_count']==0
        assert repairs.result(store,'task',defer(summary)) is None
        assert plans.active_plan(store,'task') is None
        receipt=repairs.repair(store,'task',**defer(summary))
        assert receipt['draft_indices']==[1] and receipt['document_changed'] is False
        assert receipt['draft_semantics_verified'] is False
        assert len(receipt['references'])==1
        assert plans.resolve(store,'task',receipt['references'][0]['ref'],role='header')=='Header'
        state=repairs._get(store,'task',summary['ref'])
        assert state['original_request']==plans.PlanContent(**original).model_dump()
        assert state['original_hash']==digest(canonical(state['original_request']).encode())
        assert request==original
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        draft=repairs.draft_summary(store,'task')
        assert draft['items'][0]['preview']=='A newly drafted sentence.'
        assert draft['items'][0]['fresh'] is True and draft['items'][0]['source']=='objective'
        assert draft['omitted']==0 and len(canonical(draft).encode())<=600
        assert repairs.pending(store,'task')['status']=='completed'
    finally:store.close()


def test_replace_retains_unchanged_occurrence_and_role(tmp_path):
    store=fixture(tmp_path,'Header Header Correct')
    try:
        request,summary=failed(store,[{'role':'header','quote':'Header','occurrence':1},
                                     {'role':'paragraph','quote':'Wrong'}])
        result=repairs.repair(store,'task',ref=summary['ref'],changes=[{
            'op':'replace','index':1,'selection':{'role':'footer','quote':'Correct'}}])
        item=json.loads(store.db.execute('SELECT body FROM word_content_refs WHERE ref=?',
                                        (result['references'][0]['ref'],)).fetchone()[0])
        assert item['role']=='header' and item['start']==7
        assert result['references'][1]['role']=='footer'
        assert repairs.draft_summary(store,'task') is None
    finally:store.close()


def test_failed_correction_is_atomic_and_does_not_consume_handle(tmp_path):
    store=fixture(tmp_path,'Header')
    try:
        _,summary=failed(store);before=database(store)
        with pytest.raises(HarnessError):
            repairs.repair(store,'task',ref=summary['ref'],changes=[{
                'op':'replace','index':1,'selection':{'role':'paragraph','quote':'Still absent'}}])
        assert database(store)==before
        assert repairs.repair(store,'task',**defer(summary))['status']=='repaired'
    finally:store.close()


@pytest.mark.parametrize('mode',['extend','replace'])
@pytest.mark.parametrize('existing',[False,True])
def test_all_draft_explicit_membership_and_generation(tmp_path,mode,existing):
    store=fixture(tmp_path,'Header Next')
    try:
        if existing:plans.plan(store,'task',literals=[{'role':'header','quote':'Header'}])
        previous=plans.active_plan(store,'task')
        _,summary=failed(store,[{'role':'paragraph','quote':'Draft only'}],plan_mode=mode)
        result=repairs.repair(store,'task',**defer(summary,0))
        current=plans.active_plan(store,'task')
        if mode=='extend':assert current==previous
        else:
            assert current['refs']==[]
            assert current['generation']==(previous['generation']+1 if previous else 1)
        assert result['references']==[] and result['draft_indices']==[0]
        plans.plan(store,'task',literals=[{'role':'paragraph','quote':'Next'}])
        assert plans.active_plan(store,'task')['generation']==(current['generation']+1 if current else 1)
    finally:store.close()


@pytest.mark.parametrize('change',['source','active','target','supersede'])
def test_pending_freshness_and_supersession(tmp_path,change):
    store=fixture(tmp_path,'Header Next')
    try:
        _,summary=failed(store)
        if change=='source':store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        elif change=='active':plans.plan(store,'task',literals=[{'role':'paragraph','quote':'Next'}])
        elif change=='target':store.db.execute("UPDATE tasks SET artifact='different.docx' WHERE id='task'")
        else:failed(store,[{'role':'paragraph','quote':'Another draft'}])
        if change!='supersede':
            assert repairs.pending(store,'task')['status']=='stale'
            assert 'invalid' not in repairs.pending(store,'task')
        before=database(store)
        with pytest.raises(HarnessError):repairs.repair(store,'task',**defer(summary))
        assert database(store)==before
    finally:store.close()


def test_completed_replay_and_result_after_restart_never_resolve_or_plan(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Header')
    request,summary=failed(store);args=defer(summary)
    receipt=repairs.repair(store,'task',**args)
    store.db.execute("UPDATE tasks SET objective='Changed source' WHERE id='task'")
    store.db.commit();store.close();store=ProjectStore(tmp_path)
    try:
        def forbidden(*args,**kwargs):pytest.fail('Receipt replay must not resolve or execute')
        monkeypatch.setattr(plans,'plan',forbidden)
        monkeypatch.setattr(plans,'source',forbidden)
        monkeypatch.setattr(plans,'resolve',forbidden)
        before=database(store)
        assert repairs.result(store,'task',args)==receipt
        assert repairs.repair(store,'task',**args)==receipt
        assert repairs.failure_result(store,'task',request)['code']=='content_quote'
        assert database(store)==before
        other={**args,'changes':[{'op':'defer_as_draft','index':0}]}
        assert repairs.result(store,'task',other) is None
        with pytest.raises(HarnessError) as error:repairs.repair(store,'task',**other)
        assert error.value.code=='idempotency_conflict'
    finally:store.close()


def test_failure_receipt_exact_snapshot_and_supersession(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Header')
    try:
        request,summary=failed(store)
        failure=repairs.failure_result(store,'task',request)
        assert failure['recovery']['plan_repair']==summary
        assert len(canonical(failure).encode())<=2048
        assert repairs.capture(store,'task',request,HarnessError('content_quote','different prose'))==summary
        assert repairs.failure_result(store,'task',request)==failure
        failed(store,[{'role':'paragraph','quote':'Different missing text'}])
        assert repairs.failure_result(store,'task',request) is None
    finally:store.close()


@pytest.mark.parametrize('api',['pending','repair','result','failure_result','draft_summary'])
def test_owner_checks(tmp_path,api):
    store=fixture(tmp_path,'Header')
    try:
        request,summary=failed(store);repairs.repair(store,'task',**defer(summary))
        select_chat(store,create_chat(store))
        with pytest.raises(HarnessError):
            if api=='repair':repairs.repair(store,'task',**defer(summary))
            elif api=='result':repairs.result(store,'task',defer(summary))
            elif api=='failure_result':repairs.failure_result(store,'task',request)
            else:getattr(repairs,api)(store,'task')
    finally:store.close()


@pytest.mark.parametrize('part',['envelope','original','receipt','failure'])
def test_hash_verification_precedes_replay(tmp_path,part):
    store=fixture(tmp_path,'Header')
    try:
        _,summary=failed(store);args=defer(summary);repairs.repair(store,'task',**args)
        key=repairs.PREFIX+summary['ref']
        envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
        if part=='original':envelope['state']['original_request']['literals'][0]['quote']='Tampered'
        elif part=='receipt':envelope['state']['receipt']['draft_indices']=[]
        elif part=='failure':envelope['state']['failure_receipt']['message']='Tampered'
        else:envelope['state']['status']='pending'
        if part!='envelope':envelope['hash']=digest(canonical(envelope['state']).encode())
        store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),key))
        with pytest.raises(HarnessError) as error:repairs.result(store,'task',args)
        assert error.value.code=='state_corrupt'
    finally:store.close()


def test_unicode_summary_failure_and_draft_bounds_explicit_omissions(tmp_path):
    store=fixture(tmp_path,'Header')
    try:
        choices=[{'role':'paragraph','quote':'漢😀'*81+str(n)} for n in range(24)]
        request,summary=failed(store,choices)
        assert len(canonical(summary).encode())<=1024
        assert len(summary['invalid'])+summary['omitted_invalid_count']==24
        assert summary['truncated'] and summary['omitted_invalid_count']>0
        for entry in summary['invalid']:
            assert choices[entry['index']]['quote'].startswith(entry['preview'])
            assert entry['truncated']
        failure=repairs.failure_result(store,'task',request)
        assert len(canonical(failure).encode())<=2048
        receipt=repairs.repair(store,'task',ref=summary['ref'],changes=[
            {'op':'defer_as_draft','index':n} for n in range(24)])
        assert len(canonical(receipt).encode())<=2048
        draft=repairs.draft_summary(store,'task')
        assert len(canonical(draft).encode())<=600
        assert len(draft['items'])+draft['omitted']==24
        store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        stale=repairs.draft_summary(store,'task')
        assert all(item['fresh'] is False and 'preview' not in item for item in stale['items'])
    finally:store.close()


@pytest.mark.parametrize('changes',[
    [],[{'op':'defer_as_draft','index':-1}],[{'op':'defer_as_draft','index':24}],
    [{'op':'defer_as_draft','index':0}]*2,
    [{'op':'remove','index':0}],
    [{'op':'replace','index':0,'selection':{'role':'paragraph','quote':'x'*8001}}],
])
def test_closed_bounded_change_contract(changes):
    with pytest.raises(ValidationError):repairs.RepairContentPlan(ref='cp_'+'a'*32,changes=changes)


def test_capture_filters_and_invalid_index_are_nonmutating(tmp_path):
    store=fixture(tmp_path,'Header')
    try:
        before=database(store)
        assert repairs.capture(store,'task',{},HarnessError('content_quote','bad')) is None
        assert repairs.capture(store,'task',{'literals':[{'role':'header','quote':'Header'}]},HarnessError('content_quote','bad')) is None
        assert repairs.capture(store,'task',{},HarnessError('state_corrupt','bad')) is None
        assert database(store)==before
        _,summary=failed(store);before=database(store)
        with pytest.raises(HarnessError):repairs.repair(store,'task',**defer(summary,23))
        assert database(store)==before
    finally:store.close()


def test_message_source_occurrence_error_is_repaired_without_drafting(tmp_path):
    store=fixture(tmp_path,'Use supplied source')
    try:
        message=store.message('user','Exact passage','task')
        request,summary=failed(store,[{'role':'paragraph','quote':'Exact passage','occurrence':2}],source_message=message)
        assert summary['invalid'][0]['reason']=='occurrence_out_of_range'
        receipt=repairs.repair(store,'task',ref=summary['ref'],changes=[{'op':'replace','index':0,
            'selection':{'role':'paragraph','quote':'Exact passage','occurrence':0}}])
        assert receipt['draft_indices']==[]
        evidence=json.loads(store.db.execute('SELECT body FROM word_content_refs WHERE ref=?',
                                            (receipt['references'][0]['ref'],)).fetchone()[0])
        assert evidence['source']==f'message:{message}'
        assert repairs.draft_summary(store,'task') is None
    finally:store.close()


def test_failure_and_draft_corruption_never_silently_disappear(tmp_path):
    store=fixture(tmp_path,'Header')
    try:
        request,summary=failed(store)
        repairs.repair(store,'task',**defer(summary))
        store.db.execute('UPDATE meta SET value=? WHERE key=?',('{}',repairs.DRAFT_PREFIX+'task'))
        with pytest.raises(HarnessError) as error:repairs.draft_summary(store,'task')
        assert error.value.code=='state_corrupt'
        key=repairs.PREFIX+summary['ref']
        store.db.execute('UPDATE meta SET value=? WHERE key=?',('{}',key))
        with pytest.raises(HarnessError) as error:repairs.failure_result(store,'task',request)
        assert error.value.code=='state_corrupt'
    finally:store.close()


def test_receipt_save_failure_rolls_back_plan_and_drafts(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Header')
    try:
        _,summary=failed(store);before=database(store)
        save=repairs._save
        def interrupted(store,key,state):
            save(store,key,state)
            if state.get('status')=='completed':raise RuntimeError('interrupted')
        monkeypatch.setattr(repairs,'_save',interrupted)
        with pytest.raises(RuntimeError):repairs.repair(store,'task',**defer(summary))
        assert database(store)==before
    finally:store.close()


def test_request_and_failure_message_byte_bounds(tmp_path):
    store=fixture(tmp_path,'Header')
    try:
        large=[{'op':'replace','index':n,'selection':{'role':'paragraph','quote':'漢'*8000}} for n in range(2)]
        with pytest.raises(ValidationError):repairs.RepairContentPlan(ref='cp_'+'a'*32,changes=large)
        request={'literals':[{'role':'paragraph','quote':'Missing'}]}
        hint=repairs.capture(store,'task',request,HarnessError('content_quote','漢😀'*1200))
        failure=repairs.failure_result(store,'task',request)
        assert failure['recovery']['plan_repair']==hint
        assert failure['recovery']['message_truncated'] is True
        assert 'message_truncated' not in {k for k in failure}
        assert len(canonical(failure).encode())<=2048
    finally:store.close()


def native_failure(store,request,call):
    with pytest.raises(HarnessError) as error:plans.plan(store,'task',**request)
    return repairs.capture(store,'task',request,error.value,native_call_id=call)


def test_same_arguments_new_unexecuted_call_has_no_failure_association(tmp_path):
    store=fixture(tmp_path,'Header')
    request={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Missing'}]}
    try:
        first=record_call(store,'task',request)
        summary=native_failure(store,request,first)
        receipt=repairs.failure_result(store,'task',request,native_call_id=first)
        record_result(store,'task',first,receipt)
        old=store.db.execute('SELECT value FROM meta WHERE key=?',(repairs.FAILURE_PREFIX+first,)).fetchone()[0]
        second=record_call(store,'task',request)
        before=database(store)
        assert repairs.failure_result(store,'task',request,native_call_id=second) is None
        assert database(store)==before
        # Only actual failed execution creates B's association, even with reused cp_.
        assert native_failure(store,request,second)==summary
        assert repairs.failure_result(store,'task',request,native_call_id=second)==receipt
        assert repairs.failure_result(store,'task',request,native_call_id=first)==receipt
        assert store.db.execute('SELECT value FROM meta WHERE key=?',(repairs.FAILURE_PREFIX+first,)).fetchone()[0]==old
        assert store.db.execute('SELECT count(*) FROM meta WHERE key LIKE ?',
                                (repairs.FAILURE_PREFIX+'%',)).fetchone()[0]==2
    finally:store.close()


def test_call_association_survives_supersession_and_source_changes_without_resolution(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Header')
    request={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Missing'}]}
    try:
        call=record_call(store,'task',request);native_failure(store,request,call)
        receipt=repairs.failure_result(store,'task',request,native_call_id=call)
        record_result(store,'task',call,receipt)
        failed(store,[{'role':'paragraph','quote':'Another missing selection'}])
        assert repairs.failure_result(store,'task',request) is None
        store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        def forbidden(*args,**kwargs):pytest.fail('Historical failure recovery cannot resolve or plan')
        monkeypatch.setattr(plans,'source',forbidden)
        monkeypatch.setattr(plans,'resolve',forbidden)
        monkeypatch.setattr(plans,'plan',forbidden)
        before=database(store)
        assert repairs.failure_result(store,'task',request,native_call_id=call)==receipt
        assert database(store)==before
    finally:store.close()


@pytest.mark.parametrize('mismatch',['unknown','wrong_tool','wrong_args','wrong_task','completed'])
def test_capture_rejects_invalid_call_association_atomically(tmp_path,mismatch):
    store=fixture(tmp_path,'Header')
    request={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Missing'}]}
    try:
        action=copy.deepcopy(request);key='task'
        if mismatch=='wrong_tool':action={'tool':'finish','text':'Done'}
        if mismatch=='wrong_args':action['literals'][0]['quote']='Different'
        if mismatch=='wrong_task':task(store,'other');key='other'
        call=record_call(store,key,action)
        if mismatch=='unknown':call='call_'+'0'*32
        if mismatch=='completed':record_result(store,'task',call,{'code':'content_quote'})
        before=database(store)
        with pytest.raises(HarnessError) as error:
            repairs.capture(store,'task',request,HarnessError('content_quote','Missing'),native_call_id=call)
        assert error.value.code=='native_history'
        assert database(store)==before
        if mismatch!='completed':
            with pytest.raises(HarnessError):repairs.failure_result(store,'task',request,native_call_id=call)
    finally:store.close()


@pytest.mark.parametrize('part',['envelope','receipt','request_hash','repair_record'])
def test_native_association_tampering_fails_closed(tmp_path,part):
    store=fixture(tmp_path,'Header')
    request={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Missing'}]}
    try:
        call=record_call(store,'task',request);summary=native_failure(store,request,call)
        key=repairs.FAILURE_PREFIX+call
        if part=='repair_record':key=repairs.PREFIX+summary['ref']
        envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
        if part=='request_hash':envelope['state']['request_hash']='0'*64
        elif part=='repair_record':envelope['state']['original_request']['literals'][0]['quote']='Changed'
        else:envelope['state']['failure_receipt']['message']='Changed'
        if part!='envelope':envelope['hash']=digest(canonical(envelope['state']).encode())
        with store.transaction():
            store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),key))
        before=database(store)
        with pytest.raises(HarnessError) as error:
            repairs.failure_result(store,'task',request,native_call_id=call)
        assert error.value.code=='state_corrupt'
        assert database(store)==before
    finally:store.close()


def test_native_association_write_is_atomic_with_new_capture(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Header')
    request={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Missing'}]}
    try:
        call=record_call(store,'task',request);before=database(store);save=repairs._save
        def interrupted(store,key,state):
            save(store,key,state)
            if key.startswith(repairs.FAILURE_PREFIX):raise RuntimeError('interrupted')
        monkeypatch.setattr(repairs,'_save',interrupted)
        with pytest.raises(RuntimeError):native_failure(store,request,call)
        assert database(store)==before
    finally:store.close()
