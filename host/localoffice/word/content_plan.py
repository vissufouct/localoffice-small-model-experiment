"""Exact source quotations for literal content; no document writes or intent inference."""
import json
import re
from typing import Literal
from pydantic import Field,model_validator
from .contracts import Contract
from ..chats import current_chat
from ..errors import HarnessError
from ..storage import canonical,digest


class LiteralSelection(Contract):
    role: Literal['paragraph','header','footer','table_cell'] = Field(description="Destination for this literal. For a header/footer containing page fields, select only its literal prefix here; page-number fields and their separator are configured by create.")
    quote: str = Field(min_length=1,max_length=8000,description="COPIED source passages ONLY, verbatim. Never newly drafted content or instructions. Requested drafting goes later in plain creation text, not quote. One selection per supplied literal paragraph or label.")
    occurrence: int | None = Field(default=None,ge=0,le=1000,description="Usually omit: identical source matches are preserved together. Only set a zero-based occurrence when a particular source location matters; this is not the selection's list index.")

    @model_validator(mode='after')
    def destination_limit(self):
        if self.role!='paragraph' and len(self.quote)>2000:
            raise ValueError(f'{self.role} literals must not exceed the creation slot limit of 2000 characters.')
        return self


class PlanContent(Contract):
    tool: Literal['word.plan_content'] = 'word.plan_content'
    source_message: int | None = Field(default=None,ge=1)
    plan_mode: Literal['extend','replace'] = Field(default='extend',description="Extend the explicitly active selections, or replace them with exactly this call's selections. Replacement preserves historical references.")
    literals: list[LiteralSelection] = Field(min_length=1,max_length=24)


ACTIVE_PLAN_PREFIX='word_active_literal_plan:'


def _plan_owner(store,task_id):
    row=store.db.execute('SELECT c.chat_id,t.artifact FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE t.id=?',(task_id,)).fetchone()
    if not row or row['chat_id']!=current_chat(store):
        raise HarnessError('chat_scope','Active literal plans belong to their original task and chat.')
    return {'task_id':task_id,'chat_id':row['chat_id'],'target':row['artifact']}


def _active_state(store,task_id,*,allow_stale=False):
    owner=_plan_owner(store,task_id)
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+task_id,)).fetchone()
    if row is None:return None
    try:
        envelope=json.loads(row[0]);state=envelope['state']
        if envelope['hash']!=digest(canonical(state).encode()):raise ValueError('hash')
        if set(state)!={'version','generation','task_id','chat_id','target','refs','reference_hashes'}:raise ValueError('fields')
        if type(state['version']) is not int or state['version']!=1:raise ValueError('version')
        if type(state['generation']) is not int or not 1<=state['generation']<2**63:raise ValueError('generation')
        refs=state['refs']
        # Explicit all-drafting replacement can clear active membership while
        # preserving every historical reference and the generation ledger.
        if not isinstance(refs,list) or not 0<=len(refs)<=512:raise ValueError('refs')
        for item in refs:
            if not isinstance(item,dict) or set(item)!={'ref','role'}:raise ValueError('entry')
            if not isinstance(item['ref'],str) or not re.fullmatch(r'ct_[0-9a-f]{24}',item['ref']):raise ValueError('ref')
            if item['role'] not in ('paragraph','header','footer','table_cell'):raise ValueError('role')
        keys=[item['ref'] for item in refs]
        if len(set(keys))!=len(keys) or not isinstance(state['reference_hashes'],dict) or set(state['reference_hashes'])!=set(keys):raise ValueError('reference hashes')
        if any(not isinstance(h,str) or not re.fullmatch(r'[0-9a-f]{64}',h) for h in state['reference_hashes'].values()):raise ValueError('reference hash')
    except (ValueError,TypeError,KeyError) as exc:
        raise HarnessError('state_corrupt','Active literal plan failed its integrity check.') from exc
    if any(state[key]!=value for key,value in owner.items()):
        raise HarnessError('content_plan_scope','Active literal plan task, chat or target changed.')
    for item in state['refs']:
        saved=store.db.execute('SELECT body FROM word_content_refs WHERE ref=? AND task_id=?',(item['ref'],task_id)).fetchone()
        if saved is None or digest(saved[0].encode())!=state['reference_hashes'][item['ref']]:
            raise HarnessError('state_corrupt','Active literal reference evidence is missing or changed.')
        try:resolve(store,task_id,item['ref'],role=item['role'])
        except HarnessError as exc:
            if not allow_stale or exc.code not in ('content_reference','content_source'):raise
    return state


def active_plan(store,task_id):
    """Return the explicit active state, validating owner, evidence and sources.

    version is the envelope schema; generation is the membership revision.
    No record means historical references are not implicitly made mandatory.
    """
    with store.transaction():return _active_state(store,task_id)


def _activate(store,task_id,previous,refs,mode):
    if previous is not None and previous['refs']==refs:return previous
    state={'version':1,'generation':previous['generation']+1 if previous else 1,
           **_plan_owner(store,task_id),'refs':refs,'reference_hashes':{}}
    for item in refs:
        body=store.db.execute('SELECT body FROM word_content_refs WHERE ref=? AND task_id=?',(item['ref'],task_id)).fetchone()[0]
        state['reference_hashes'][item['ref']]=digest(body.encode())
    revision=digest(canonical(state).encode())
    node=f'literal-plan:{task_id}:{state["generation"]}'
    # Immutable revisions retain prior membership and exact reference hashes.
    old=store.db.execute('SELECT body FROM nodes WHERE id=?',(node,)).fetchone()
    if old and old[0]!=canonical(state):raise HarnessError('state_corrupt','Literal plan revision identity conflict.')
    if previous is not None:
        prior=f'literal-plan:{task_id}:{previous["generation"]}'
        prior_body=canonical(previous);prior_hash=digest(prior_body.encode())
        old=store.db.execute('SELECT body FROM nodes WHERE id=?',(prior,)).fetchone()
        if old and old[0]!=prior_body:raise HarnessError('state_corrupt','Prior literal plan revision changed.')
        store.node(prior,'literal_plan_revision',previous,prior_hash)
        store.edge(node,prior,'supersedes-literal-plan','explicit:'+mode)
    store.node(node,'literal_plan_revision',state,revision)
    store.edge('task:'+task_id,node,'activated-literal-plan','explicit:'+mode)
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(ACTIVE_PLAN_PREFIX+task_id,canonical({'state':state,'hash':revision})))
    return state


def quote_evidence(text,quote):
    """Bounded lexical evidence, never an inferred replacement or saved selection."""
    # Only inspect a short prefix and at most twelve word boundaries. A hit
    # explains where a failed literal diverges, not what the user intended.
    ends=[match.end() for match in re.finditer(r'\S+\s*',quote[:80])][:12]
    for end in reversed(ends):
        prefix=quote[:end]
        if len(prefix.strip())<3 or prefix==quote:continue
        start=text.find(prefix)
        if start<0:continue
        excerpt=text[max(0,start-24):start+len(prefix)+48]
        return {'exact_prefix':prefix,'nearby_source':excerpt}
    return None


def quote_hint(text,quote):
    evidence=quote_evidence(text,quote)
    return ' Source evidence (not an automatic replacement): '+canonical(evidence)+'.' if evidence else ''


def _quote_recovery(revision,validated,invalid):
    """Bounded diagnostics, never a generated plan or inferred drafting choice."""
    result={'kind':'selection_review','source_hash':revision,'validated_indices':validated,
            'validation_scope':'quote_and_occurrence','invalid':[],
            'omitted_invalid_count':len(invalid),'truncated':bool(invalid),
            'instruction':'Source matching applies only to copied passages; requested drafting needs no source quote and may be generated as plain creation text. No selections saved. Resubmit validated selections unchanged. For not_in_source only, decide: requested drafting or quotation needing correction. Keep drafting for creation; do not drop uncertain requested literals. Fix occurrence errors separately. Do not reconfirm supplied text.'}
    for diagnostic in invalid:
        quote=diagnostic['quote']
        entry={key:value for key,value in diagnostic.items() if key!='quote'}
        entry.update(quote_preview=quote[:80],quote_truncated=len(quote)>80)
        while True:
            omitted=len(invalid)-len(result['invalid'])-1
            trial={**result,'invalid':result['invalid']+[entry],'omitted_invalid_count':omitted,
                   'truncated':omitted>0 or entry['quote_truncated'] or any(item['quote_truncated'] for item in result['invalid'])}
            if len(canonical(trial).encode())<=1024:
                result=trial;break
            if not entry['quote_preview']:break
            entry['quote_preview']=entry['quote_preview'][:len(entry['quote_preview'])//2]
            entry['quote_truncated']=True
    return result


def source(store,task_id,message_id=None):
    chat=current_chat(store)
    row=store.db.execute('SELECT t.objective FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE t.id=? AND c.chat_id=?',(task_id,chat)).fetchone()
    if not row:raise HarnessError('chat_scope','Content references require a task in the active chat.')
    if message_id is None:
        text=row[0];identity='objective'
    else:
        row=store.db.execute("SELECT m.content FROM messages m JOIN message_chats c ON c.message_id=m.id WHERE m.id=? AND m.task_id=? AND c.chat_id=? AND m.role='user'",(message_id,task_id,chat)).fetchone()
        if not row:raise HarnessError('content_source','Choose a user message from this task and chat.')
        text=row[0];identity='message:'+str(message_id)
    if len(text.encode())>65536:raise HarnessError('content_source','Content source exceeds the bounded planning limit.')
    return identity,text,digest(text.encode())


def plan(store,task_id,**arguments):
    request=PlanContent(**arguments)
    if len(canonical(request.model_dump()).encode())>32768:
        raise HarnessError('content_plan_limit','Literal plan exceeds 32 KiB; plan another bounded section.')
    with store.transaction():
        previous=_active_state(store,task_id,allow_stale=request.plan_mode=='replace')
        identity,text,revision=source(store,task_id,request.source_message)
        prepared=[];errors=[];validated_indices=[];invalid=[]
        corrected=request.model_dump(exclude_none=True);prefix_repairs=0
        occurrence_correction=request.model_dump(exclude_unset=True)
        occurrence_correction['tool']='word.plan_content'
        occurrence_repairs=0
        for index,selection in enumerate(request.literals):
            positions=[];offset=0
            while len(positions)<=1001:
                position=text.find(selection.quote,offset)
                if position<0:break
                positions.append(position);offset=position+1
            field=f'literals.{index}'
            if not positions:
                hint=quote_hint(text,selection.quote) if len(errors)<3 else ''
                evidence=quote_evidence(text,selection.quote)
                if evidence:
                    prefix=evidence['exact_prefix'];position=text.find(prefix)
                    if selection.occurrence in (None,0) and text.find(prefix,position+1)<0:
                        corrected['literals'][index]['quote']=prefix
                        corrected['literals'][index]['occurrence']=0
                        prefix_repairs+=1
                errors.append(('content_quote',f'{field}.quote ({selection.role}): not an exact passage. Select contiguous source text; do not join words separated by instructions or fields.'+hint))
                invalid.append({'index':index,'role':selection.role,'reason':'not_in_source','quote':selection.quote})
                continue
            if selection.occurrence is None and len(positions)!=1:
                if len(positions)<=32:
                    spans=[[p,p+len(selection.quote)] for p in positions]
                    key='ct_'+digest(canonical([task_id,identity,revision,selection.role,spans]).encode())[:24]
                    prepared.append({'ref':key,'role':selection.role,'text':selection.quote,'source':identity,'source_hash':revision,'spans':spans})
                    validated_indices.append(index)
                    continue
                errors.append(('content_ambiguous',f'{field}.occurrence ({selection.role}): quote has more than 32 matches; supply a zero-based occurrence from 0 to {min(len(positions)-1,1000)}.'))
                invalid.append({'index':index,'role':selection.role,'reason':'occurrence_required','quote':selection.quote})
                continue
            occurrence=selection.occurrence if selection.occurrence is not None else 0
            if occurrence>=len(positions):
                if len(positions)==1:
                    occurrence_correction['literals'][index]['occurrence']=0
                    occurrence_repairs+=1
                errors.append(('content_quote',f'{field}.occurrence ({selection.role}): requested occurrence does not exist; valid range is 0 to {len(positions)-1}.'))
                invalid.append({'index':index,'role':selection.role,'reason':'occurrence_out_of_range','quote':selection.quote})
                continue
            start=positions[occurrence];end=start+len(selection.quote)
            key='ct_'+digest(canonical([task_id,identity,revision,selection.role,start,end]).encode())[:24]
            prepared.append({'ref':key,'role':selection.role,'text':selection.quote,'source':identity,'source_hash':revision,'start':start,'end':end})
            validated_indices.append(index)
        if errors:
            # Report every invalid selection in the bounded request before any write.
            # A repair should not spend another model call discovering a masked error.
            recovery=None
            selections=corrected['literals']
            if (len(errors)==occurrence_repairs and
                    all(item['reason']=='occurrence_out_of_range' for item in invalid)):
                candidate={'candidate_only':True,'source_hash':revision,
                           'instruction':'Each invalid occurrence has exactly one exact-source match (index 0). Occurrence is a source-match index, not a band or selection ordinal. Review and explicitly resubmit candidate_call; quotes, roles and valid selections are unchanged. Nothing has been saved or executed. Source matching is revalidated on resubmission.',
                           'candidate_call':occurrence_correction}
                if len(canonical(candidate).encode())<=2048:recovery=candidate
            if recovery is None and len(errors)==prefix_repairs==1 and len({(x['role'],x['quote'],x.get('occurrence',0)) for x in selections})==len(selections):
                candidate={'candidate_only':True,'source_hash':revision,
                           'instruction':'Compare this exact-source prefix candidate with the task yourself and resubmit candidate_call when consistent. No user approval is needed for source-grounded planning; do not ask to confirm text already supplied. Page fields and their separator belong to creation layout. Nothing has been saved or executed.',
                           'candidate_call':corrected}
                if len(canonical(candidate).encode())<=2048:recovery=candidate
            if recovery is None and any(item['reason']=='not_in_source' for item in invalid):
                recovery=_quote_recovery(revision,validated_indices,invalid)
            raise HarnessError(errors[0][0],'No references saved. Correct these selections and preserve valid selections: '+ ' '.join(message for _,message in errors),recovery=recovery)
        if len({item['ref'] for item in prepared})!=len(prepared):
            raise HarnessError('content_duplicate','Select each source passage and role once; a reference may be reused later.')
        active_refs=list(previous['refs']) if previous is not None and request.plan_mode=='extend' else []
        known={item['ref'] for item in active_refs}
        for item in prepared:
            if item['ref'] not in known:
                active_refs.append({'ref':item['ref'],'role':item['role']});known.add(item['ref'])
        if len(active_refs)>512:raise HarnessError('content_plan_limit','Active literal plan exceeds 512 references; explicitly replace the plan to change its membership.')
        count=store.db.execute('SELECT count(*) FROM word_content_refs WHERE task_id=?',(task_id,)).fetchone()[0]
        existing=sum(bool(store.db.execute('SELECT 1 FROM word_content_refs WHERE ref=? AND task_id=?',(item['ref'],task_id)).fetchone()) for item in prepared)
        if count+len(prepared)-existing>512:
            raise HarnessError('content_plan_limit','Task content-reference limit of 512 reached; replacement does not discard historical references.')
        for item in prepared:
            body=canonical(item)
            old=store.db.execute('SELECT body FROM word_content_refs WHERE ref=?',(item['ref'],)).fetchone()
            if old and old[0]!=body:raise HarnessError('state_corrupt','Content reference identity conflict.')
            store.db.execute('INSERT OR IGNORE INTO word_content_refs VALUES(?,?,?)',(item['ref'],task_id,body))
            store.node('content:'+item['ref'],'literal_reference',item,revision)
            store.edge('task:'+task_id,'content:'+item['ref'],'selected-literal','source:'+identity+':'+revision)
        active=_activate(store,task_id,previous,active_refs,request.plan_mode)
        return {'status':'planned','document_changed':False,'source_hash':revision,'literals':prepared,
                'active_plan':{'version':active['version'],'generation':active['generation'],'count':len(active['refs'])},
                'limitation':'Exact source binding does not prove these passages fully represent the user intent.'}


def resolve(store,task_id,ref,*,role):
    row=store.db.execute('SELECT body FROM word_content_refs WHERE ref=? AND task_id=?',(ref,task_id)).fetchone()
    if not row:raise HarnessError('content_reference','Unknown content reference for this task.')
    try:
        item=json.loads(row[0])
        if not isinstance(item,dict) or item.get('ref')!=ref:
            raise ValueError('reference identity')
        if not isinstance(item.get('text'),str) or not 1<=len(item['text'])<=8000:
            raise ValueError('literal text')
        origin=item.get('source')
        if origin!='objective' and (not isinstance(origin,str) or not re.fullmatch(r'message:[1-9][0-9]*',origin)):
            raise ValueError('source identity')
        message_id=None if origin=='objective' else int(origin.split(':')[1])
        if not isinstance(item.get('source_hash'),str) or 'role' not in item:
            raise ValueError('source binding')
    except (ValueError,TypeError) as exc:
        raise HarnessError('state_corrupt','Malformed content reference; plan it again.') from exc
    identity,text,revision=source(store,task_id,message_id)
    if item['role']!=role or item['source_hash']!=revision:
        raise HarnessError('content_reference','Content role or source revision changed; plan it again.')
    if 'spans' in item:
        spans=item['spans']
        if ('start' in item or 'end' in item or not isinstance(spans,list) or not 2<=len(spans)<=32
                or any(not isinstance(span,list) or len(span)!=2 or any(type(n) is not int for n in span) for span in spans)):
            raise HarnessError('state_corrupt','Malformed source match locations; plan it again.')
        positions=[];offset=0
        while len(positions)<=32:
            start=text.find(item['text'],offset)
            if start<0:break
            positions.append([start,start+len(item['text'])]);offset=start+1
        if not 2<=len(positions)<=32 or positions!=item['spans']:
            raise HarnessError('content_reference','Source match locations changed; plan it again.')
        identity_parts=[task_id,identity,revision,role,positions]
    else:
        start,end=item.get('start'),item.get('end')
        if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):
            raise HarnessError('state_corrupt','Malformed source passage bounds; plan it again.')
        if text[item['start']:item['end']]!=item['text']:
            raise HarnessError('content_reference','Source passage changed; plan it again.')
        identity_parts=[task_id,identity,revision,role,item['start'],item['end']]
    expected='ct_'+digest(canonical(identity_parts).encode())[:24]
    if expected!=ref:raise HarnessError('state_corrupt','Content reference failed its identity check.')
    return item['text']


def model_receipt(value):
    """Project a persisted plan, never replace its exact text or provenance.

    At most 24 entries with 80-character previews keep this below 16 KiB
    even for JSON-escaped control characters. Every reference is retained;
    long text is explicitly a preview and creation resolves the full value.
    """
    if not (isinstance(value,dict) and value.get('status')=='planned'
            and value.get('document_changed') is False and 'literals' in value):
        return value
    literals=value['literals']
    if not isinstance(literals,list) or not 1<=len(literals)<=24:
        return value
    # Also make projection idempotent: compact receipts are not raw plans.
    if any(not isinstance(item,dict) or not {'ref','role','text','source','source_hash'}<=item.keys() for item in literals):
        return value
    entries=[]
    for item in literals:
        entry={'ref':item['ref'],'role':item['role']}
        text=item['text']
        if len(text)<=80:entry['text']=text
        else:entry.update(preview=text[:80],text_chars=len(text),preview_truncated=True)
        entry['matches']=len(item['spans']) if 'spans' in item else 1
        entries.append(entry)
    result={'status':'planned','document_changed':False,
            'source':literals[0]['source'],'source_hash':value['source_hash'],
            'literals':entries,'limitation':value['limitation'],
            'binding':'Put {"ref":"ct_..."} directly in the text slot or table cell. Previews are navigation only; binding uses complete exact text. Active selected refs must be retained before completion; change membership only by explicit plan replacement.'}
    if 'active_plan' in value:result['active_plan']=dict(value['active_plan'])
    return result


def _inline_slots(request):
    """Only declared creation text slots may carry inline references."""
    from .contracts import LiteralTextReference
    for index,block in enumerate(request.blocks):
        if block.kind=='paragraph' and isinstance(block.text,LiteralTextReference):
            yield f'blocks.{index}.text',block.text.ref
        elif block.kind=='table':
            for row,cells in enumerate(block.rows):
                for column,value in enumerate(cells):
                    if isinstance(value,LiteralTextReference):
                        yield f'blocks.{index}.rows.{row}.{column}',value.ref
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        band=getattr(request,name)
        if band is not None and isinstance(band.text,LiteralTextReference):
            yield name+'.text',band.text.ref


def has_bindings(request):
    """Detect legacy or inline references without changing the submitted request."""
    return bool(request.literal_refs) or next(_inline_slots(request),None) is not None


def bind_creation(store,task_id,request):
    """Bind explicit slots and validate the complete executable design."""
    from .contracts import CreateDocument
    return _bind_creation(store,task_id,request,CreateDocument)


def bind_creation_shape(store,task_id,request):
    """Review only: validate sources, resolved slot limits and hard safety guards."""
    from .contracts import CreateDocumentShape
    return _bind_creation(store,task_id,request,CreateDocumentShape)


def _bind_creation(store,task_id,request,model):
    import re
    data=request.model_dump(exclude={'literal_refs'})
    refs=dict(request.literal_refs)
    for slot,ref in _inline_slots(request):
        if slot in refs:
            raise HarnessError('content_conflict','A text slot cannot have both an inline reference and a legacy literal_refs binding, even to the same reference.')
        refs[slot]=ref
        # Generated slot paths only; do not mutate the submitted model or map.
        parent=data
        parts=slot.split('.')
        for part in parts[:-1]:parent=parent[int(part)] if isinstance(parent,list) else parent[part]
        parent[int(parts[-1]) if isinstance(parent,list) else parts[-1]]=''
    if len(refs)>24:
        raise HarnessError('content_plan_limit','Creation supports at most 24 combined inline and legacy literal bindings.')
    bindings=[]
    for slot,ref in refs.items():
        if slot in ('header.text','footer.text','first_header.text','first_footer.text','even_header.text','even_footer.text'):
            band=slot.split('.')[0]
            parent=data.get(band)
            if parent is None:raise HarnessError('content_slot','Referenced header/footer must be declared.')
            key='text';role=band.rsplit('_',1)[-1]
        elif match:=re.fullmatch(r'blocks\.(0|[1-9][0-9]*)\.text',slot):
            index=int(match[1])
            if index>=len(data['blocks']) or data['blocks'][index]['kind']!='paragraph':
                raise HarnessError('content_slot','Text reference must address an existing paragraph block.')
            parent=data['blocks'][index];key='text';role='paragraph'
        elif match:=re.fullmatch(r'blocks\.(0|[1-9][0-9]*)\.rows\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',slot):
            block,row,column=map(int,match.groups())
            if block>=len(data['blocks']) or data['blocks'][block]['kind']!='table':
                raise HarnessError('content_slot','Cell reference must address a declared table block.')
            rows=data['blocks'][block]['rows']
            if row>=len(rows) or column>=len(rows[row]):raise HarnessError('content_slot','Referenced cell is outside the declared table.')
            parent=rows[row];key=column;role='table_cell'
        else:raise HarnessError('content_slot','References may bind paragraph/header/footer text or table cells only.')
        text=resolve(store,task_id,ref,role=role)
        if parent[key] not in ('',text):
            raise HarnessError('content_conflict','A referenced slot must contain empty text or the exact source quote, not competing prose.')
        parent[key]=text
        evidence=json.loads(store.db.execute('SELECT body FROM word_content_refs WHERE ref=? AND task_id=?',(ref,task_id)).fetchone()[0])
        bindings.append({'slot':slot,**evidence})
    # Revalidate the resolved document, including XML-safe text and slot limits.
    return model(**data),bindings


def summary(store,task_id,*,max_chars=1800):
    """Bounded reference navigation; full text and provenance remain on disk."""
    source(store,task_id)
    # Scan past stale/corrupt entries up to the task's 512-reference cap.
    # One extra row makes overflow explicit even in an inconsistent database.
    rows=store.db.execute('SELECT ref,body FROM word_content_refs WHERE task_id=? ORDER BY rowid DESC LIMIT 513',(task_id,))
    items=[];truncated=False
    for index,row in enumerate(rows):
        if index==512:truncated=True;break
        try:
            item=json.loads(row[1])
            if not isinstance(item,dict) or item.get('ref')!=row[0]:continue
            role=item.get('role')
            if not isinstance(role,str) or role not in ('paragraph','header','footer','table_cell'):continue
            text=resolve(store,task_id,row[0],role=role)
        except (HarnessError,ValueError,TypeError,OverflowError):continue
        entry={'ref':row[0],'role':role,'preview':text[:80],'truncated':len(text)>80}
        if len(items)==24 or len(canonical([*items,entry]))>max_chars:truncated=True;break
        items.append(entry)
    return {'references':list(reversed(items)),'truncated':truncated}


def binding_navigation(store,task_id,*,max_bytes=1200):
    """Compact current references for explicit binding decisions, never pair slots.

    Group shared roles and omit repeated per-entry keys. Exact text is shown only
    when short; long excerpts are explicitly marked. All references are resolved
    against current ownership/source/membership before exposing them.
    """
    if type(max_bytes) is not int or max_bytes<100:
        raise HarnessError('content_plan_limit','Binding navigation needs at least 100 bytes.')
    max_bytes=min(max_bytes,1200)
    with store.transaction():
        state=_active_state(store,task_id,allow_stale=True)
        if state is None:return None
        result={'count':len(state['refs']),'by_role':{},'truncated':False}
        texts=[]
        # Finalization validates the whole active plan. Check even references
        # beyond the display limits before offering any binding choices.
        for item in state['refs']:
            try:text=resolve(store,task_id,item['ref'],role=item['role'])
            except HarnessError as exc:
                if exc.code not in ('content_reference','content_source'):raise
                return {**result,'status':'plan_repair_required','truncated':True}
            texts.append(text)
        for index,(item,text) in enumerate(zip(state['refs'],texts)):
            if index==24:
                result['truncated']=True;break
            value=text if len(text)<=80 else {'preview':text[:80],'truncated':True}
            role=result['by_role'].setdefault(item['role'],{})
            role[item['ref']]=value
            if len(canonical(result).encode('utf-8'))>max_bytes:
                del role[item['ref']]
                if not role:del result['by_role'][item['role']]
                result['truncated']=True;break
        return result


def active_navigation(store,task_id,*,max_chars=640):
    """Bound active membership after continuation; never mix historical references."""
    with store.transaction():
        state=_active_state(store,task_id,allow_stale=True)
        if state is None:return None
        result={'generation':state['generation'],'count':len(state['refs']),'references':[],'truncated':False}
        for item in state['refs']:
            entry=dict(item)
            try:
                text=resolve(store,task_id,item['ref'],role=item['role'])
                entry.update(preview=text[:40],truncated=len(text)>40)
            except HarnessError as exc:
                if exc.code not in ('content_reference','content_source'):raise
                entry['status']='stale'
            proposed={**result,'references':result['references']+[entry]}
            if len(canonical(proposed))>max_chars:
                result['truncated']=True;break
            result['references'].append(entry)
        return result
