"""Explicit repair of retained selections; no inferred drafting or document writes."""
import json
import re
import uuid
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from ..errors import HarnessError
from ..storage import canonical, digest
from .contracts import Contract
from . import content_plan as plans

PREFIX='word_content_plan_repair:'
PENDING_PREFIX='word_pending_content_plan:'
DRAFT_PREFIX='word_draft_decisions:'
FAILURE_PREFIX='word_content_plan_failure:'
REF_PATTERN=r'^cp_[0-9a-f]{32}$'


class DeferAsDraft(Contract):
    op: Literal['defer_as_draft']
    index: int = Field(ge=0,le=23)


class ReplaceSelection(Contract):
    op: Literal['replace']
    index: int = Field(ge=0,le=23)
    selection: plans.LiteralSelection


class RepairContentPlan(Contract):
    tool: Literal['word.repair_content_plan']='word.repair_content_plan'
    ref: str = Field(pattern=REF_PATTERN)
    changes: list[Annotated[DeferAsDraft | ReplaceSelection,Field(discriminator='op')]] = Field(min_length=1,max_length=24)

    @model_validator(mode='after')
    def bounded_unique(self):
        indices=[change.index for change in self.changes]
        if len(indices)!=len(set(indices)):raise ValueError('Change each original selection index at most once.')
        if len(canonical(self.model_dump()).encode())>32768:raise ValueError('Repair request exceeds 32 KiB.')
        return self


def _hash(value):
    return digest(canonical(value).encode())


def _save(store,key,state):
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                     (key,canonical({'state':state,'hash':_hash(state)})))


def _read(store,key):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    if row is None:return None
    try:
        envelope=json.loads(row[0]);state=envelope['state']
        if not isinstance(state,dict) or envelope['hash']!=_hash(state):raise ValueError('hash')
        return state
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('state_corrupt','Content-plan repair metadata failed its integrity check.') from exc


def _owner(store,task_id):
    return plans._plan_owner(store,task_id)


def _check_owner(store,task_id,state):
    owner=_owner(store,task_id)
    if any(state.get(key)!=owner[key] for key in ('task_id','chat_id')):
        raise HarnessError('content_plan_scope','Content-plan repair belongs to its original task and chat.')
    return owner


def _get(store,task_id,ref):
    _owner(store,task_id)
    if not isinstance(ref,str) or not re.fullmatch(REF_PATTERN,ref):
        raise HarnessError('content_plan_repair_reference','Unknown content-plan repair.')
    state=_read(store,PREFIX+ref)
    if state is None:return None
    _check_owner(store,task_id,state)
    try:
        if state['version']!=1 or state['ref']!=ref:raise ValueError('identity')
        if _hash(state['original_request'])!=state['original_hash']:raise ValueError('original hash')
        if _hash(state['failure_receipt'])!=state['failure_hash']:raise ValueError('failure hash')
        if state['status'] not in ('pending','completed'):raise ValueError('status')
        if state['status']=='completed':
            if _hash(state['repair_request'])!=state['repair_hash']:raise ValueError('repair hash')
            if _hash(state['receipt'])!=state['receipt_hash']:raise ValueError('receipt hash')
    except (KeyError,TypeError,ValueError) as exc:
        raise HarnessError('state_corrupt','Content-plan repair record is invalid.') from exc
    return state


def _pointer(store,task_id):
    pointer=_read(store,PENDING_PREFIX+task_id)
    if pointer is None:return None
    _check_owner(store,task_id,pointer)
    if not isinstance(pointer.get('ref'),str) or not re.fullmatch(REF_PATTERN,pointer['ref']):
        raise HarnessError('state_corrupt','Invalid pending content-plan pointer.')
    state=_get(store,task_id,pointer['ref'])
    if state is None:raise HarnessError('state_corrupt','Pending content-plan repair is missing.')
    return state


def _baseline(store,task_id,request):
    active=plans._active_state(store,task_id,allow_stale=request['plan_mode']=='replace')
    return active,({'generation':active['generation'],'hash':_hash(active)} if active else None)


def _source_snapshot(store,task_id,request):
    identity,text,revision=plans.source(store,task_id,request['source_message'])
    _,_,objective_hash=plans.source(store,task_id)
    return text,{'identity':identity,'hash':revision,'objective_hash':objective_hash}


def _fresh(store,state):
    task_id=state['task_id']
    if _owner(store,task_id)!=state['owner']:
        raise HarnessError('content_plan_repair_stale','Task target changed; plan again.')
    current=_pointer(store,task_id)
    if current is None or current['ref']!=state['ref']:
        raise HarnessError('content_plan_repair_stale','This retained selection set was superseded.')
    _,source=_source_snapshot(store,task_id,state['original_request'])
    _,baseline=_baseline(store,task_id,state['original_request'])
    if source!=state['source'] or baseline!=state['active_plan']:
        raise HarnessError('content_plan_repair_stale','Sources or active selections changed; plan again.')


def _invalid(request,text):
    diagnostics=[]
    for index,selection in enumerate(request['literals']):
        count=0;offset=0
        while count<=1001:
            position=text.find(selection['quote'],offset)
            if position<0:break
            count+=1;offset=position+1
        occurrence=selection['occurrence']
        reason=('not_in_source' if count==0 else
                'occurrence_required' if occurrence is None and count>32 else
                'occurrence_out_of_range' if occurrence is not None and occurrence>=count else None)
        if reason:diagnostics.append({'index':index,'reason':reason})
    return diagnostics


def _summary(state,*,stale=False):
    result={'ref':state['ref'],'status':'stale' if stale else state['status'],
            'selection_count':len(state['original_request']['literals'])}
    if stale:
        result['instruction']='Sources or active plan changed; submit a fresh plan. Do not reuse this handle.'
    elif state['status']=='pending':
        result.update(invalid=[],omitted_invalid_count=len(state['invalid']),truncated=False,document_changed=False,
                      instruction='You choose: replace copied selections, or defer_as_draft for requested drafting, by original index. Deferral removes only the quote requirement; generate the requested text during creation. Retain unchanged selections.')
        for diagnostic in state['invalid']:
            selection=state['original_request']['literals'][diagnostic['index']]
            entry={**diagnostic,'role':selection['role'],'preview':selection['quote'][:80],
                   'truncated':len(selection['quote'])>80}
            while True:
                omitted=result['omitted_invalid_count']-1
                trial={**result,'invalid':result['invalid']+[entry],'omitted_invalid_count':omitted,
                       'truncated':omitted>0 or entry['truncated'] or any(d['truncated'] for d in result['invalid'])}
                if len(canonical(trial).encode())<=1024:
                    result=trial;break
                if not entry['preview']:break
                entry['preview']=entry['preview'][:len(entry['preview'])//2];entry['truncated']=True
        result['truncated']=result['omitted_invalid_count']>0 or any(d['truncated'] for d in result['invalid'])
    return result


def _native_call(store,task_id,call_id,request,*,require_pending):
    owner=_owner(store,task_id)
    if not isinstance(call_id,str) or not re.fullmatch(r'call_[0-9a-f]{32}',call_id):
        raise HarnessError('native_history','Expected a host-issued native call identity.')
    row=store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                         (call_id,task_id,owner['chat_id'])).fetchone()
    if row is None:raise HarnessError('native_history','Native planning call does not belong to this task and chat.')
    try:
        function=json.loads(row['function'])
        if function['name']!='word.plan_content':raise ValueError('tool')
        recorded=plans.PlanContent.model_validate(function['arguments']).model_dump()
        if _hash(recorded)!=_hash(request):raise ValueError('arguments')
    except (KeyError,TypeError,ValueError) as exc:
        raise HarnessError('native_history','Native planning call has different or invalid arguments.') from exc
    if require_pending and row['result_message'] is not None:
        raise HarnessError('native_history','Cannot associate a new failure with a completed native call.')


def _associate_failure(store,state,native_call_id):
    if native_call_id is None:return
    association={'version':1,**state['owner'],'call_id':native_call_id,
                 'request_hash':state['original_hash'],'repair_ref':state['ref'],
                 'failure_receipt':state['failure_receipt'],'failure_hash':state['failure_hash']}
    key=FAILURE_PREFIX+native_call_id
    previous=_read(store,key)
    if previous is not None:
        if previous!=association:
            raise HarnessError('state_corrupt','Native planning failure association cannot be replaced.')
        return
    _save(store,key,association)


def capture(store,task_id,arguments,error,*,native_call_id=None):
    """Call only after the failed planning transaction has rolled back."""
    if not isinstance(error,HarnessError) or error.code not in ('content_quote','content_ambiguous'):return None
    try:request=plans.PlanContent.model_validate(arguments).model_dump()
    except ValidationError:return None
    if len(canonical(request).encode())>32768:return None
    with store.transaction():
        owner=_owner(store,task_id)
        if native_call_id is not None:
            _native_call(store,task_id,native_call_id,request,require_pending=True)
        text,source=_source_snapshot(store,task_id,request)
        _,baseline=_baseline(store,task_id,request)
        invalid=_invalid(request,text)
        if not invalid:return None
        current=_pointer(store,task_id)
        if (current and current['status']=='pending' and current['original_hash']==_hash(request)
                and current['source']==source and current['active_plan']==baseline and current['owner']==owner):
            _associate_failure(store,current,native_call_id)
            return _summary(current)
        ref='cp_'+uuid.uuid4().hex
        state={'version':1,**owner,'owner':owner,'ref':ref,'status':'pending',
               'original_request':request,'original_hash':_hash(request),'source':source,
               'active_plan':baseline,'invalid':invalid,'error_code':error.code}
        failure={'code':error.code,'message':str(error)[:1200],
                 'recovery':{'plan_repair':_summary(state)}}
        # Preserve every diagnostic; shorten only the redundant prose to keep
        # the retained failure bounded even with Unicode/JSON escaping.
        while len(canonical(failure).encode())>2048 and failure['message']:
            failure['message']=failure['message'][:len(failure['message'])//2]
            failure['recovery']['message_truncated']=True
        state.update(failure_receipt=failure,failure_hash=_hash(failure))
        _save(store,PREFIX+ref,state)
        _save(store,PENDING_PREFIX+task_id,{**owner,'ref':ref})
        _associate_failure(store,state,native_call_id)
        return _summary(state)


def failure_result(store,task_id,arguments,*,native_call_id=None):
    """Native reads require an exact call association; direct reads use the pointer."""
    request=plans.PlanContent.model_validate(arguments).model_dump()
    with store.transaction():
        if native_call_id is not None:
            _native_call(store,task_id,native_call_id,request,require_pending=False)
            association=_read(store,FAILURE_PREFIX+native_call_id)
            if association is None:return None
            owner=_check_owner(store,task_id,association)
            if any(association.get(key)!=value for key,value in owner.items()):
                raise HarnessError('content_plan_scope','Failure association belongs to its original target.')
            try:
                if association['version']!=1 or association['call_id']!=native_call_id:raise ValueError('identity')
                if association['request_hash']!=_hash(request):raise ValueError('request hash')
                if association['failure_hash']!=_hash(association['failure_receipt']):raise ValueError('receipt hash')
                state=_get(store,task_id,association['repair_ref'])
                if (state is None or state['original_hash']!=association['request_hash']
                        or state['failure_hash']!=association['failure_hash']
                        or state['owner']!=owner):raise ValueError('repair provenance')
            except (KeyError,TypeError,ValueError) as exc:
                raise HarnessError('state_corrupt','Native planning failure association is invalid.') from exc
            return association['failure_receipt']
        state=_pointer(store,task_id)
        if state is None or state['original_hash']!=_hash(request):return None
        if _owner(store,task_id)!=state['owner']:
            raise HarnessError('content_plan_scope','Failure receipt belongs to its original target.')
        return state['failure_receipt']


def pending(store,task_id):
    with store.transaction():
        _owner(store,task_id)
        state=_pointer(store,task_id)
        if state is None:return None
        if state['status']=='completed':return _summary(state)
        try:_fresh(store,state)
        except HarnessError as exc:
            if exc.code not in ('content_plan_repair_stale','content_reference','content_source'):raise
            return _summary(state,stale=True)
        return _summary(state)


def result(store,task_id,arguments):
    """Exact completed receipt only; never resolve sources or execute planning."""
    request=RepairContentPlan.model_validate(arguments).model_dump()
    with store.transaction():
        state=_get(store,task_id,request['ref'])
        if state is None:return None
        if _owner(store,task_id)!=state['owner']:
            raise HarnessError('content_plan_scope','Repair receipt belongs to its original target.')
        if state['status']!='completed' or state['repair_hash']!=_hash(request):return None
        return state['receipt']


def _draft_record(store,task_id):
    record=_read(store,DRAFT_PREFIX+task_id)
    if record is not None:
        _check_owner(store,task_id,record)
        if not isinstance(record.get('items'),list) or type(record.get('omitted')) is not int:
            raise HarnessError('state_corrupt','Invalid draft-decision record.')
    return record


def repair(store,task_id,**arguments):
    request=RepairContentPlan.model_validate(arguments).model_dump()
    with store.transaction():
        state=_get(store,task_id,request['ref'])
        if state is None:raise HarnessError('content_plan_repair_reference','Unknown content-plan repair.')
        if state['status']=='completed':
            receipt=result(store,task_id,request)
            if receipt is None:raise HarnessError('idempotency_conflict','This repair was completed with different changes.')
            return receipt
        _fresh(store,state)
        original=state['original_request'];changes={item['index']:item for item in request['changes']}
        if any(index>=len(original['literals']) for index in changes):
            raise HarnessError('content_plan_repair_index','Index must identify an original selection.')
        literals=[];drafts=[]
        for index,selection in enumerate(original['literals']):
            change=changes.get(index)
            if change and change['op']=='defer_as_draft':
                drafts.append({'index':index,'role':selection['role'],'text':selection['quote']})
            else:literals.append(change['selection'] if change else selection)
        if literals:
            corrected={**original,'literals':literals}
            planned=plans.plan(store,task_id,**corrected)
            active=planned['active_plan']
            references=[{'ref':item['ref'],'role':item['role']} for item in planned['literals']]
        else:
            previous,_=_baseline(store,task_id,original)
            if original['plan_mode']=='replace':
                previous=plans._activate(store,task_id,previous,[],'replace')
            active=({'version':previous['version'],'generation':previous['generation'],'count':len(previous['refs'])}
                    if previous else None)
            references=[]
        receipt={'status':'repaired','ref':state['ref'],'document_changed':False,
                 'active_plan':active,'references':references,'draft_indices':[item['index'] for item in drafts],
                 'draft_semantics_verified':False,
                 'instruction':'Only copied selections were source-bound. Deferred text is an unbound model decision, not document content or verified user intent.'}
        # At most 24 reference/role entries: remove whole entries if needed.
        receipt['omitted_references']=0
        while len(canonical(receipt).encode())>2048:
            receipt['references'].pop();receipt['omitted_references']+=1
        state.update(status='completed',repair_request=request,repair_hash=_hash(request),
                     receipt=receipt,receipt_hash=_hash(receipt),drafts=drafts)
        _save(store,PREFIX+state['ref'],state)
        if drafts:
            record=_draft_record(store,task_id) or {**state['owner'],'items':[],'omitted':0}
            if any(record.get(key)!=state['owner'][key] for key in state['owner']):
                raise HarnessError('content_plan_scope','Draft decisions belong to their original target.')
            record['items'] += [{'ref':state['ref'],'index':item['index']} for item in drafts]
            # Full decisions remain in immutable completed repair records.
            overflow=max(0,len(record['items'])-512)
            record['items']=record['items'][overflow:];record['omitted']+=overflow
            _save(store,DRAFT_PREFIX+task_id,record)
        return receipt


def draft_summary(store,task_id):
    """Bounded unbound model decisions; fresh provenance is not semantic approval."""
    with store.transaction():
        owner=_owner(store,task_id);record=_draft_record(store,task_id)
        if record is None:return None
        result={'items':[],'omitted':record['omitted']+len(record['items']),
                'scope':'Unbound model draft decisions; not document content or verified intent.'}
        for item in reversed(record['items']):
            state=_get(store,task_id,item['ref'])
            if state is None or state['status']!='completed':
                raise HarnessError('state_corrupt','Draft decision has no completed repair.')
            draft=next((d for d in state['drafts'] if d['index']==item['index']),None)
            if draft is None:raise HarnessError('state_corrupt','Draft selection is missing.')
            fresh=owner==state['owner']
            try:
                _,source=_source_snapshot(store,task_id,state['original_request'])
                fresh=fresh and source==state['source']
            except HarnessError as exc:
                if exc.code not in ('content_source','content_reference'):raise
                fresh=False
            entry={**item,'role':draft['role'],'source':state['source']['identity'],
                   'source_hash':state['source']['hash'],'fresh':fresh}
            if fresh:
                entry.update(preview=draft['text'][:80],truncated=len(draft['text'])>80)
            while True:
                trial={**result,'items':result['items']+[entry],'omitted':result['omitted']-1}
                if len(canonical(trial).encode())<=600:
                    result=trial;break
                if not entry.get('preview'):break
                entry['preview']=entry['preview'][:len(entry['preview'])//2];entry['truncated']=True
        return result
