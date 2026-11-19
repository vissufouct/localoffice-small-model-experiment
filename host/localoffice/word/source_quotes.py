"""Finite exact quoted-source choices; roles and selections remain model decisions."""
import json
import re
from typing import Annotated, Literal

from pydantic import Field

from ..errors import HarnessError
from ..storage import canonical, digest
from .contracts import Contract
from . import content_plan as plans

PREFIX='word_source_quote_selection:'
ROLE=Literal['paragraph','header','footer','table_cell']


class SourceQuoteSelection(Contract):
    tool: Literal['word.select_source_quotes']='word.select_source_quotes'
    source_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    selections: dict[Annotated[str,Field(pattern=r'^q(?:0[1-9]|1[0-9]|2[0-4])$')],ROLE] = Field(min_length=1,max_length=24)


def _hash(value):
    return digest(canonical(value).encode())


def _unavailable(reason,revision):
    return {'available':False,'source_hash':revision,'candidates':{},'omitted':True,
            'reason':reason,'fallback':'word.plan_content'}


def _scan(text):
    """No escaping or nesting grammar is guessed. Bounds use Unicode code points."""
    spans=[];opening=None;start=None
    for index,char in enumerate(text):
        if char not in ('"','“','”'):continue
        if index and text[index-1]=='\\':return None,'escaped_delimiter'
        if opening is None:
            if char=='”':return None,'unbalanced_quotes'
            if index and text[index-1] in ('"','“','”'):return None,'ambiguous_quotes'
            opening=char;start=index+1
        else:
            expected='"' if opening=='"' else '”'
            if char!=expected:return None,'ambiguous_quotes'
            if not 1<=index-start<=8000:return None,'passage_length'
            spans.append((start,index))
            if len(spans)>24:return None,'candidate_limit'
            opening=None;start=None
    if opening is not None:return None,'unbalanced_quotes'
    if not spans:return None,'no_quoted_passages'
    return spans,None


def _occurrence(text,quote,start):
    offset=0
    for occurrence in range(1001):
        found=text.find(quote,offset)
        if found==start:return occurrence
        if found<0 or found>start:break
        offset=found+1
    return None


def _candidates(store,task_id):
    try:_,text,revision=plans.source(store,task_id)
    except HarnessError as exc:
        if exc.code!='content_source':raise
        # source() has verified ownership before rejecting its byte bound.
        text=store.db.execute('SELECT objective FROM tasks WHERE id=?',(task_id,)).fetchone()[0]
        return _unavailable('source_limit',digest(text.encode())),None
    spans,reason=_scan(text)
    if reason:return _unavailable(reason,revision),None
    entries={};full={}
    for number,(start,end) in enumerate(spans,1):
        key=f'q{number:02d}';quote=text[start:end]
        occurrence=_occurrence(text,quote,start)
        if occurrence is None:return _unavailable('occurrence_limit',revision),None
        entries[key]={'preview':quote[:80],'truncated':len(quote)>80,'text_chars':len(quote)}
        full[key]={'quote':quote,'start':start,'end':end,'occurrence':occurrence}
    projection={'available':True,'source_hash':revision,'candidates':entries,'omitted':False}
    if len(canonical(projection).encode())>2048:return _unavailable('projection_limit',revision),None
    # Candidate completeness takes priority over optional navigation context.
    # Reserve the omission flag even at the old projection's byte boundary;
    # shorten only labelled previews, never the underlying exact quotations.
    projection['context_omitted']=False
    while len(canonical(projection).encode())>2048:
        entry=max(entries.values(),key=lambda item:len(item['preview'].encode()))
        entry['preview']=entry['preview'][:-1];entry['truncated']=True
    context_budget=(2048-len(canonical(projection).encode()))//len(entries)
    for key,item in full.items():
        delimiter=item['start']-1
        context=text[max(0,delimiter-128):delimiter]
        # Share the remaining byte budget equally, so early quotes do not
        # consume all navigation context for later passages. Source bytes and
        # roles remain untouched; context is never selected as document text.
        before=len(canonical(entries[key]).encode())
        entries[key]['preceding_noncontent']=context
        while context and len(canonical(entries[key]).encode())-before>context_budget:
            context=context[1:]
            entries[key]['preceding_noncontent']=context
        if not context:
            del entries[key]['preceding_noncontent']
            projection['context_omitted']=True
    return projection,full


def candidates(store,task_id):
    """All candidates or explicit unavailability; no inferred roles or partial list."""
    with store.transaction():return _candidates(store,task_id)[0]


def _native_call(store,task_id,native_call_id,request):
    owner=plans._plan_owner(store,task_id)
    if not isinstance(native_call_id,str) or not re.fullmatch(r'call_[0-9a-f]{32}',native_call_id):
        raise HarnessError('native_history','Expected a host-issued native call identity.')
    row=store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                         (native_call_id,task_id,owner['chat_id'])).fetchone()
    if row is None:raise HarnessError('native_history','Source selection call belongs to another task or chat.')
    try:
        function=json.loads(row['function'])
        if function['name']!='word.select_source_quotes':raise ValueError('tool')
        submitted=SourceQuoteSelection.model_validate(function['arguments']).model_dump()
        if _hash(submitted)!=_hash(request):raise ValueError('request')
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('native_history','Source selection call has different or invalid arguments.') from exc
    return owner,row


def _receipt(store,owner,native_call_id,request):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+native_call_id,)).fetchone()
    if row is None:return None
    try:
        envelope=json.loads(row[0]);state=envelope['state']
        if envelope['hash']!=_hash(state):raise ValueError('envelope')
        if state['version']!=1 or state['call_id']!=native_call_id:raise ValueError('identity')
        if state['request_hash']!=_hash(state['request']):raise ValueError('request hash')
        if state['receipt_hash']!=_hash(state['receipt']):raise ValueError('receipt hash')
    except (ValueError,TypeError,KeyError) as exc:
        raise HarnessError('state_corrupt','Source selection receipt failed its integrity check.') from exc
    if state.get('owner')!=owner:
        raise HarnessError('content_plan_scope','Source selection receipt belongs to its original task, chat and target.')
    if state['request_hash']!=_hash(request):
        raise HarnessError('native_history','Source selection receipt records different arguments.')
    return state['receipt']


def result(store,task_id,args,*,native_call_id):
    """Historical exact receipt, not current source acceptance; no source resolution."""
    request=SourceQuoteSelection.model_validate(args).model_dump()
    with store.transaction():
        owner,_=_native_call(store,task_id,native_call_id,request)
        return _receipt(store,owner,native_call_id,request)


def select(store,task_id,*,source_hash,selections,native_call_id=None):
    request=SourceQuoteSelection(source_hash=source_hash,selections=selections).model_dump()
    with store.transaction():
        owner=plans._plan_owner(store,task_id)
        if native_call_id is not None:
            owner,row=_native_call(store,task_id,native_call_id,request)
            receipt=_receipt(store,owner,native_call_id,request)
            if receipt is not None:return receipt
            if row['result_message'] is not None:
                raise HarnessError('native_history','Completed native call has no recorded source-selection receipt.')
        projection,full=_candidates(store,task_id)
        if projection.get('source_hash')!=source_hash:
            raise HarnessError('content_source_stale','Task source changed; inspect current source candidates.')
        if not projection['available']:
            raise HarnessError('source_quotes_unavailable','Exact quoted-source selection is unavailable; use word.plan_content.')
        if any(key not in full for key in selections):
            raise HarnessError('source_quote_reference','Select only current quoted-source candidate IDs.')
        literals=[{'role':role,'quote':full[key]['quote'],'occurrence':full[key]['occurrence']}
                  for key,role in selections.items()]
        receipt=plans.plan(store,task_id,literals=literals)
        if native_call_id is not None:
            state={'version':1,'owner':owner,'call_id':native_call_id,
                   'request':request,'request_hash':_hash(request),
                   'receipt':receipt,'receipt_hash':_hash(receipt)}
            store.db.execute('INSERT INTO meta VALUES(?,?)',
                             (PREFIX+native_call_id,canonical({'state':state,'hash':_hash(state)})))
        return receipt
