"""Model-declared numeric requirements with exact source provenance, not intent proof."""
import json
import re
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Annotated,Literal
from pydantic import Field,model_validator,model_serializer
from ..errors import HarnessError
from ..storage import canonical,digest
from .contracts import Contract
from .content_plan import _plan_owner,source

PREFIX='word_requirements:'
HISTORY_PREFIX='word_requirements_history:'
RESULT_PREFIX='word_requirements_result:'
PLANNING_MODES=frozenset({'requirements_reviewed_source_planning',
    'focused_drafting_v1:requirements_reviewed_source_planning',
    'isolated_draft_planning_v1:focused_drafting_v1:requirements_reviewed_source_planning'})
Quote=Annotated[str,Field(min_length=1,max_length=8000)]
Index=Annotated[int,Field(strict=True,ge=0,le=23)]
Number=Annotated[float,Field(strict=True,allow_inf_nan=False)]


def twips(value,unit):
    # Enough precision for every finite IEEE float plus exact decimal unit
    # arithmetic before the single documented integer rounding step.
    with localcontext() as context:
        context.prec=400
        factors={'twip':Decimal(1),'in':Decimal(1440),'cm':Decimal(72000)/Decimal(127),'pt':Decimal(20)}
        return int((Decimal(str(value))*factors[unit]).quantize(Decimal(1),rounding=ROUND_HALF_UP))


class SourceRange(Contract):
    start: str=Field(pattern=r'^s(?:0[1-9]|1[0-9]|2[0-4])$')
    end: str=Field(pattern=r'^s(?:0[1-9]|1[0-9]|2[0-4])$')


class RequirementEvidence(Contract):
    source_quote: Quote | None = None
    source_range: SourceRange | None = None

    @model_validator(mode='after')
    def exclusive_source(self):
        if (self.source_quote is None)==(self.source_range is None):
            raise ValueError('Supply exactly one of source_quote or source_range.')
        return self

    @model_serializer(mode='wrap')
    def sparse_evidence(self,handler):
        return {key:value for key,value in handler(self).items() if value is not None}


class PageGeometry(RequirementEvidence):
    kind: Literal['page_geometry']
    field: Literal['width','height','margin']
    equals: Number
    unit: Literal['twip','in','cm','pt']

    @model_validator(mode='after')
    def effective_bounds(self):
        value=twips(self.equals,self.unit)
        low,high=(0,4320) if self.field=='margin' else (2880,31680)
        if not low<=value<=high:raise ValueError('Converted page quantity exceeds PageSettings bounds.')
        return self


class ParagraphWords(RequirementEvidence):
    kind: Literal['paragraph_words']
    paragraph_index: Index
    min: Annotated[int,Field(strict=True,ge=0,le=8000)]
    max: Annotated[int,Field(strict=True,ge=0,le=8000)]

    @model_validator(mode='after')
    def ordered(self):
        if self.min>self.max:raise ValueError('Word minimum must not exceed maximum.')
        return self


class ParagraphFontSize(RequirementEvidence):
    kind: Literal['paragraph_font_size']
    paragraph_index: Index
    equals: Annotated[float,Field(strict=True,ge=6,le=144,allow_inf_nan=False)]

    @model_validator(mode='after')
    def half_points(self):
        if Decimal(str(self.equals))*2 % 1:raise ValueError('Font size requires exact half-point increments.')
        return self


class ParagraphPageBreak(RequirementEvidence):
    kind: Literal['paragraph_page_break']
    paragraph_index: Index
    equals: Annotated[bool,Field(strict=True)]


Requirement=Annotated[PageGeometry|ParagraphWords|ParagraphFontSize|ParagraphPageBreak,Field(discriminator='kind')]


class PlanRequirements(Contract):
    tool: Literal['word.plan_requirements']='word.plan_requirements'
    source_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    requirements: list[Requirement]=Field(max_length=8)

    @model_validator(mode='after')
    def bounded(self):
        if len(self.model_dump_json().encode())>32768:raise ValueError('Requirement plan exceeds 32 KiB.')
        return self


def _hash(value):return digest(canonical(value).encode())


def normalize(args):return PlanRequirements.model_validate(args).model_dump(exclude_none=True)


def _read_envelope(store,key):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    if row is None:return None
    try:
        envelope=json.loads(row[0]);state=envelope['state']
        if envelope['hash']!=_hash(state):raise ValueError('hash')
        return state
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('state_corrupt','Requirement evidence failed its integrity check.') from exc


def _write(store,key,state):
    store.db.execute('INSERT INTO meta VALUES(?,?)',(key,canonical({'state':state,'hash':_hash(state)})))


def _validate(state,owner):
    try:
        if state['version'] not in (1,2) or type(state['generation']) is not int or state['generation']<1:raise ValueError('version')
        if state['request_hash']!=_hash(state['request']):raise ValueError('request hash')
        if normalize(state['request'])!=state['request']:raise ValueError('normalization')
        text=state['source_text']
        if not isinstance(text,str) or len(text.encode())>65536:raise ValueError('source bound')
        if digest(text.encode())!=state['request']['source_hash']:raise ValueError('source hash')
        entries=state['request']['requirements'];spans=state['source_spans']
        if len(entries)!=len(spans):raise ValueError('spans')
        has_ranges=any('source_range' in entry for entry in entries)
        if has_ranges != (state['version']==2):raise ValueError('evidence version')
        if has_ranges:
            from .objective_evidence import catalogue_text,resolve_text
            catalogue=state['source_catalogue']
            if catalogue.get('version')!=1:raise ValueError('catalogue version')
            if catalogue_text(text,version=1)!=catalogue:raise ValueError('catalogue')
        for entry,span in zip(entries,spans):
            if 'source_range' in entry:
                selected=entry['source_range']
                try:resolved=resolve_text(text,state['request']['source_hash'],selected['start'],selected['end'],version=1)
                except HarnessError as exc:raise ValueError('range') from exc
                expected={'start':resolved['start'],'end':resolved['end']}
            else:
                quote=entry['source_quote'];start=text.find(quote)
                if start<0 or text.find(quote,start+1)>=0:raise ValueError('quote')
                expected={'start':start,'end':start+len(quote)}
            if span!=expected:raise ValueError('span')
        if state['owner']!=owner:raise HarnessError('requirements_scope','Requirement plan belongs to another task, chat or target.')
    except (ValueError,TypeError,KeyError) as exc:
        raise HarnessError('state_corrupt','Requirement plan failed its integrity check.') from exc


def get(store,task_id):
    """Verified active generation; stale sources are reported by the checker."""
    owner=_plan_owner(store,task_id)
    pointer=_read_envelope(store,PREFIX+task_id)
    if pointer is None:return None
    try:
        generation=pointer['generation']
        if type(generation) is not int or generation<1:raise ValueError('generation')
        state=_read_envelope(store,HISTORY_PREFIX+task_id+':'+str(generation))
        if state is None or pointer['plan_hash']!=_hash(state):raise ValueError('plan hash')
        if state['generation']!=generation or state['version'] not in (1,2):raise ValueError('identity')
        if state['request_hash']!=_hash(state['request']):raise ValueError('request')
        _validate(state,owner)
        return state
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('state_corrupt','Requirement plan failed its integrity check.') from exc


def _call(store,task_id,call_id,request):
    owner=_plan_owner(store,task_id)
    if not isinstance(call_id,str) or not re.fullmatch(r'call_[0-9a-f]{32}',call_id):
        raise HarnessError('native_history','Expected a host-issued requirement call identity.')
    row=store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                        (call_id,task_id,owner['chat_id'])).fetchone()
    if row is None:raise HarnessError('native_history','Requirement call belongs to another task or chat.')
    try:
        function=json.loads(row['function'])
        if function['name']!='word.plan_requirements' or normalize(function['arguments'])!=request:
            raise ValueError('arguments')
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('native_history','Requirement call has different arguments.') from exc
    return owner,row


def _result(store,task_id,call_id,request):
    owner,_=_call(store,task_id,call_id,request)
    saved=_read_envelope(store,RESULT_PREFIX+call_id)
    if saved is None:return None
    try:
        if saved['owner']!=owner or saved['call_id']!=call_id or saved['request']!=request:raise ValueError('identity')
        if saved['request_hash']!=_hash(saved['request']) or saved['receipt_hash']!=_hash(saved['receipt']):raise ValueError('hash')
        historical=_read_envelope(store,HISTORY_PREFIX+task_id+':'+str(saved['receipt']['generation']))
        if historical is None or _hash(historical)!=saved['plan_hash']:raise ValueError('history')
        _validate(historical,owner)
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('state_corrupt','Requirement receipt failed its integrity check.') from exc
    return saved['receipt']


def result(store,task_id,args,*,native_call_id):
    """Historical exact call evidence, not current source acceptance."""
    request=normalize(args)
    with store.transaction():return _result(store,task_id,native_call_id,request)


def plan(store,task_id,*,source_hash,requirements,native_call_id=None):
    request=normalize({'source_hash':source_hash,'requirements':requirements})
    with store.transaction():
        owner=_plan_owner(store,task_id)
        if not owner['target'] or not owner['target'].lower().endswith('.docx'):
            raise HarnessError('requirements_scope','Requirements must target the active Word artifact.')
        if native_call_id is not None:
            owner,row=_call(store,task_id,native_call_id,request)
            prior=_result(store,task_id,native_call_id,request)
            if prior is not None:return prior
            if row['result_message'] is not None:raise HarnessError('native_history','Completed call has no requirement receipt.')
        previous=get(store,task_id)
        _,objective,current_hash=source(store,task_id)
        if source_hash!=current_hash:raise HarnessError('requirements_stale','Requirement source hash is stale.')
        spans=[];invalid=[];catalogue=None
        for index,entry in enumerate(request['requirements']):
            if 'source_range' in entry:
                from .objective_evidence import catalogue as current_catalogue,resolve
                if catalogue is None:catalogue=current_catalogue(store,task_id)
                selected=entry['source_range']
                resolved=resolve(store,task_id,source_hash,selected['start'],selected['end'],version=1)
                spans.append({'start':resolved['start'],'end':resolved['end']})
                continue
            quote=entry['source_quote'];start=objective.find(quote)
            if start<0 or objective.find(quote,start+1)>=0:
                invalid.append({'index':index,'reason':'not_found' if start<0 else 'ambiguous'})
            spans.append({'start':start,'end':start+len(quote)})
        if invalid:
            raise HarnessError('requirements_quote','Each source quote must match exactly once in the current objective.',
                recovery={'invalid_quotes':invalid,'index_base':0,'document_changed':False,
                    'hint':'Replace these source_quote values with short, unique verbatim spans from the objective. Do not add literal backslashes or paraphrase. Resubmit the full plan; nothing was saved.'})
        generation=previous['generation']+1 if previous else 1
        state={'version':2 if catalogue is not None else 1,'generation':generation,'owner':owner,'request':request,
               'request_hash':_hash(request),'source_spans':spans,'source_text':objective,
               'supersedes':{'generation':previous['generation'],'plan_hash':_hash(previous)} if previous else None}
        if catalogue is not None:state['source_catalogue']=catalogue
        _write(store,HISTORY_PREFIX+task_id+':'+str(generation),state)
        pointer={'generation':generation,'plan_hash':_hash(state)}
        store.db.execute('DELETE FROM meta WHERE key=?',(PREFIX+task_id,));_write(store,PREFIX+task_id,pointer)
        receipt={'status':'planned' if requirements else 'unassessed','generation':generation,
                 'count':len(requirements),'source_hash':source_hash,'coverage':'unassessed',
                 'interpretation':'model_selected','document_changed':False,
                 'supersedes_generation':previous['generation'] if previous else None}
        if native_call_id is not None:
            saved={'owner':owner,'call_id':native_call_id,'request':request,'request_hash':_hash(request),
                   'receipt':receipt,'receipt_hash':_hash(receipt),'plan_hash':_hash(state)}
            _write(store,RESULT_PREFIX+native_call_id,saved)
        return receipt


def require_planned(store,task_id):
    """Opt-in planning prerequisite only; an empty plan is not acceptance."""
    if task_id is None:return None
    with store.transaction():
        mode=store.db.execute('SELECT value FROM meta WHERE key=?',('task_content_mode:'+task_id,)).fetchone()
        if mode is None or mode[0] not in PLANNING_MODES:return None
        state=get(store,task_id)
        _,_,current=source(store,task_id)
        reason='missing' if state is None else 'stale' if state['request']['source_hash']!=current else None
        if reason:
            raise HarnessError('requirements_planning_required',
                'Requirements planning is '+reason+'. Load plan_requirements and explicitly submit a plan for the current objective; an empty plan remains unassessed.',
                recovery={'tool':'word.load_tools','operations':['plan_requirements'],
                          'reason':reason,'coverage':'unassessed'})
        return state
