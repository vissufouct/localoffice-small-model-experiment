"""Durable missing-style repair with explicit source bindings; no worker dispatch."""
import json
import re
import uuid
from pydantic import ValidationError
from ..chats import current_chat
from ..errors import HarnessError
from ..paths import project_path
from ..storage import canonical,digest
from .contracts import CreateDocument,CreateDocumentShape,WordStyle
from .content_plan import bind_creation,bind_creation_shape

PREFIX='word_creation_repair:'
FRESHNESS_ERRORS=frozenset({'creation_repair_stale','content_reference','content_source'})


def _hash(value):
    return digest(canonical(value).encode())


def _owner(store,task_id):
    row=store.db.execute('SELECT t.objective,t.artifact,c.chat_id FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE t.id=?',(task_id,)).fetchone()
    if not row or row['chat_id']!=current_chat(store):
        raise HarnessError('creation_repair_scope','Creation repair belongs to its original task and chat.')
    return dict(row)


def _sources(store,task_id):
    return [{'id':row['id'],'hash':digest(row['content'].encode())} for row in store.db.execute(
        "SELECT m.id,m.content FROM messages m JOIN message_chats c ON c.message_id=m.id WHERE m.task_id=? AND c.chat_id=? AND m.role='user' ORDER BY m.id",(task_id,current_chat(store)))]


def _absent(store,path):
    target=project_path(store.root,path)
    if target.exists() or store.candidate(path):
        raise HarnessError('creation_repair_stale','Creation target is no longer absent; inspect current state.')


def _missing(shape):
    declared={s.id for s in shape.styles}|{'Normal'}
    uses={}
    for i,block in enumerate(shape.blocks):
        if block.kind=='paragraph' and block.style not in declared:
            uses.setdefault(block.style,[]).append(f'blocks.{i}.style')
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        band=getattr(shape,name)
        if band is not None and band.style not in declared:
            uses.setdefault(band.style,[]).append(name+'.style')
    return uses


def _save(store,state):
    envelope={'state':state,'hash':_hash(state)}
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(PREFIX+state['task_id'],canonical(envelope)))


def get(store,task_id,ref=None):
    """Integrity/ownership-checked full metadata, including after staging.

    Does not assert source freshness or target absence: reconciliation needs to
    inspect this record after a successful creation. Use resolve before execution.
    """
    owner=_owner(store,task_id)
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+task_id,)).fetchone()
    if not row:
        if ref is not None:raise HarnessError('creation_repair_reference','Unknown creation repair.')
        return None
    try:
        envelope=json.loads(row[0]);state=envelope['state']
        if envelope['hash']!=_hash(state) or state['payload_hash']!=_hash(state['payload']):raise ValueError()
        if state['version']!=1 or state['task_id']!=task_id or state['chat_id']!=owner['chat_id']:raise ValueError()
        if not re.fullmatch(r'cr_[0-9a-f]{32}',state['ref']):raise ValueError()
        if state['target_absent'] is not True:raise ValueError()
        if 'review_required' in state and type(state['review_required']) is not bool:raise ValueError()
        if state.get('resolved_request') is not None and state['resolved_hash']!=_hash(state['resolved_request']):raise ValueError()
    except (ValueError,TypeError,KeyError) as exc:
        raise HarnessError('state_corrupt','Creation repair failed its integrity check.') from exc
    if ref is not None and ref!=state['ref']:
        raise HarnessError('creation_repair_reference','Unknown or superseded creation repair.')
    return state


def _fresh(store,state):
    owner=_owner(store,state['task_id'])
    if (state['objective_hash']!=digest(owner['objective'].encode())
            or state['artifact']!=owner['artifact'] or state['sources']!=_sources(store,state['task_id'])):
        raise HarnessError('creation_repair_stale','Creation objective or user sources changed; submit a new creation proposal.')
    _absent(store,state['payload']['path'])
    # Literal source evidence must still match even if its DB record changed.
    from .content_plan import resolve as resolve_literal
    for binding in state['literal_bindings']:
        if resolve_literal(store,state['task_id'],binding['ref'],role=binding['role'])!=binding['text']:
            raise HarnessError('creation_repair_stale','Literal source changed.')
        row=store.db.execute('SELECT body FROM word_content_refs WHERE ref=? AND task_id=?',(binding['ref'],state['task_id'])).fetchone()
        if json.loads(row[0])!={k:v for k,v in binding.items() if k!='slot'}:
            raise HarnessError('creation_repair_stale','Literal provenance changed.')


def _proposed_literals(payload,reference_like=()):
    """Bound actual proposed slots, without pairing them to source candidates."""
    slots=[]
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        if isinstance(payload.get(name),dict):slots.append((name+'.text',payload[name].get('text','')))
    for index,block in enumerate(payload['blocks']):
        if block['kind']=='paragraph':slots.append((f'blocks.{index}.text',block['text']))
        elif block['kind']=='table':
            for row,cells in enumerate(block['rows']):
                for column,value in enumerate(cells):slots.append((f'blocks.{index}.rows.{row}.{column}',value))
    result={'items':[],'omitted':False,'word_count_basis':'unicode_whitespace_split'}
    for slot,value in slots:
        reference=payload.get('literal_refs',{}).get(slot)
        if isinstance(value,dict):reference=value['ref']
        if reference:entry={'slot':slot,'ref':reference}
        elif len(value)<=80:entry={'slot':slot,'text':value}
        else:entry={'slot':slot,'preview':value[:80],'truncated':True}
        if not reference and re.fullmatch(r'blocks\.[0-9]+\.text',slot):
            entry['word_count']=len(value.split())
        if slot in reference_like:entry['reference_like']=True
        trial={**result,'items':result['items']+[entry]}
        if len(canonical(trial).encode())<=640:result=trial
        else:result['omitted']=True
    return result


def _proposed_design(payload,provided_paths=None):
    """Actual declared design plus labelled contract defaults, not rendering."""
    shape=CreateDocumentShape.model_validate(payload)
    result={'scope':'selected proposal properties; not rendered','presence':'verified' if provided_paths is not None else 'unknown','style_count':len(shape.styles),'items':[],'omitted':False}
    items=[]
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        band=getattr(shape,name)
        if band is not None:
            keys=('style','alignment','page_numbers','separator')
            item={'slot':name,**{key:getattr(band,key) for key in keys}}
            defaults=[key for key in keys if provided_paths is not None and name+'.'+key not in provided_paths]
            if defaults:item['defaults']=defaults
            items.append(item)
    if not any(style.id=='Normal' for style in shape.styles):
        items.append({'style':'Normal','font':'Arial','size_points':11,'implicit_default':True})
    for index,style in enumerate(shape.styles):
        raw=payload['styles'][index]
        size=style.size_points if style.size_points is not None else style.size_half_points/2 if style.size_half_points is not None else 11
        item={'slot':f'styles.{index}','style':style.id,'font':style.font,'size_points':size}
        defaults=[]
        if provided_paths is not None and f'styles.{index}.font' not in provided_paths:defaults.append('font')
        if provided_paths is not None and (raw.get('size_points') is None and raw.get('size_half_points') is None):defaults.append('size_points')
        if defaults:item['defaults']=defaults
        items.append(item)
    page=shape.page.model_dump()
    items.append({'page_twips':page,**({'defaults':[key for key in page if 'page.'+key not in provided_paths]} if provided_paths is not None else {})})
    for item in items:
        trial={**result,'items':result['items']+[item]}
        if len(canonical(trial).encode())<=640:result=trial
        else:result['omitted']=True
    return result


def _reference_like_slots(store,state,navigation):
    """Navigation warning only; exact active handles never imply user intent."""
    candidates=[]
    for item in navigation['items']:
        match=re.fullmatch(r'ref: (ct_[0-9a-f]{24})',item.get('text',''))
        if match:candidates.append((item,match[1]))
    if not candidates:return set()
    from .content_plan import active_plan,resolve as resolve_literal
    try:
        active=active_plan(store,state['task_id'])
        if active is None:return set()
        members={item['ref'] for item in active['refs']}
        candidates=[(item,reference) for item,reference in candidates if reference in members]
        if not candidates:return set()
        # An explicitly selected same-role source literal may itself contain
        # reference-like text. Preserve that meaning without warning.
        wanted={(repair_role,item['text']) for item,_ in candidates
                for _,_,repair_role in [_binding_slot(state['payload'],item['slot'])]}
        exempt=set()
        for selected in active['refs']:
            if any(role==selected['role'] for role,_ in wanted):
                text=resolve_literal(store,state['task_id'],selected['ref'],role=selected['role'])
                if (selected['role'],text) in wanted:exempt.add((selected['role'],text))
        return {item['slot'] for item,_ in candidates
                if (_binding_slot(state['payload'],item['slot'])[2],item['text']) not in exempt}
    except HarnessError as exc:
        # A stale source plan cannot justify a warning. Integrity and ownership
        # failures remain explicit, as elsewhere in proposal navigation.
        if exc.code in ('content_reference','content_source'):return set()
        raise


def _design_diagnostics(state):
    """Locate structural violations in retained blocks, without choosing repairs."""
    blocks=state['payload']['blocks']
    breaks=[]
    for index,block in enumerate(blocks):
        if block['kind']!='page_break':continue
        reasons=[]
        if index==0:reasons.append('leading')
        if index==len(blocks)-1:reasons.append('trailing')
        if ((index>0 and blocks[index-1]['kind']=='page_break') or
                (index+1<len(blocks) and blocks[index+1]['kind']=='page_break')):
            reasons.append('adjacent')
        if reasons:
            breaks.append({'path':f'blocks.{index}','code':'page_break_position',
                           'kind':block['kind'],'reasons':reasons,
                           'message':'Page-break block must be between content blocks.'})
    mapped_message='A page-break block must be between content blocks, without adjacent breaks.'
    errors=[*breaks]
    for error in state['design_errors']:
        if (breaks and error['path']=='$' and error['code']=='value_error'
                and error['message'] in (mapped_message,'Value error, '+mapped_message)):
            continue
        errors.append(error)
    diagnostic={'items':[],'omitted_count':len(errors)}
    for error in errors:
        entry={'path':error['path'][:100],'code':error['code'][:80],
               'message':error['message'][:240],
               'truncated':len(error['path'])>100 or len(error['code'])>80 or len(error['message'])>240}
        if error.get('code')=='page_break_position':
            entry.update(kind=error['kind'],reasons=error['reasons'])
        trial={'items':[*diagnostic['items'],entry],
               'omitted_count':diagnostic['omitted_count']-1}
        if len(canonical(trial).encode())>640:break
        diagnostic=trial
    if len(canonical({**diagnostic,'repair_tool':'word.revise_creation'}).encode())<=640:
        diagnostic['repair_tool']='word.revise_creation'
    return diagnostic


def _summary(state,store):
    prepared=state.get('review_required',False)
    result={'ref':state['ref'],'status':'prepared' if prepared else 'missing_styles','document_changed':False,
            'missing_styles':[{'id':key,'slots':slots} for key,slots in state['missing'].items()],
            'declared_styles':state['declared'],
            'proposed_literals':_proposed_literals(state['payload']),
            'instruction':'Declare exactly the missing style IDs. Optional bindings (slot/ref pairs) confirm identical text or fill empty slots using current active same-role references; revise competing content explicitly first. Without bindings, preserve all original text. Existing styles and remaining content/layout cannot change.'}
    if state.get('edit_effect_current') is True and state.get('revision_origin'):
        result['edit_effect']=state['edit_effect']
    if prepared:
        from .creation_submission import presence
        result['proposed_design']=_proposed_design(state['payload'],presence(store,state) if store is not None else None)
        blocks=state['payload']['blocks']
        result['block_layout']={
            'block_count':len(blocks),
            'paragraphs':[i for i,block in enumerate(blocks) if block['kind']=='paragraph'],
            'page_break_before':[i for i,block in enumerate(blocks)
                                 if block['kind']=='paragraph' and block.get('page_break_before',False)],
            'standalone_breaks':[i for i,block in enumerate(blocks) if block['kind']=='page_break']}
        result['instruction']='Compare requested text, word counts, typography, page geometry and breaks against these facts. Correct mismatches before finalizing; defaults and valid structure do not prove intent coverage. Geometry: 1440 twips/inch. Use revise_creation for compact content/design changes; prepare_creation replaces the whole proposal.'
    warnings=_reference_like_slots(store,state,result['proposed_literals'])
    if warnings:
        result['proposed_literals']=_proposed_literals(state['payload'],warnings)
        if any(item.get('reference_like') for item in result['proposed_literals']['items']):
            lead=('Finalize with missing styles and optional bindings; revise_creation edits, prepare_creation replaces. '
                  if prepared else 'Declare exactly missing styles; preserve other content/design. ')
            result['instruction']=lead+'reference_like is text, not binding: bind only if source insertion was intended; otherwise retain it.'
    if state.get('design_errors'):
        result['status']='invalid_design'
        result['validation_errors']=_design_diagnostics(state)
        result['instruction']='Retained proposal has invalid design. Use revise_creation with explicit changes, then review again. Finalization requires full validation; no document was created.'
    if store is not None:
        from .requirement_checks import check_prepared
        selected_checks=check_prepared(store,state['task_id'],state['payload'])
        if selected_checks is not None:
            result['selected_requirements']=selected_checks
    if result.get('edit_effect',{}).get('proposal_changed') is False:
        result['instruction']+=' Last edit changed nothing. Do not repeat unchanged edits. Review requested meaning, tone and remaining conditions; finalize_creation only if satisfied, otherwise revise actual differences. Selected numeric checks do not certify meaning.'
    result['instruction']+=' Summary fields are read-only; edit blocks/styles/page via tools.'
    return result


def capture(store,task_id,payload,allow_valid=False):
    """Retain a create-shaped proposal, optionally requiring explicit review.

    Default mode captures missing-style failures only and abstains otherwise.
    Review mode retains shape-safe proposals with explicit design errors; hard
    guards and source errors still reject retention. Neither mode dispatches a worker or invents missing declarations.
    """
    if type(allow_valid) is not bool:raise ValueError('allow_valid must be a boolean.')
    with store.transaction():
        owner=_owner(store,task_id)
        previous=get(store,task_id)  # Never replace corrupt durable evidence.
        try:
            shape=CreateDocumentShape.model_validate(payload)
            # Preserve the exact raw request only within the same hard byte bound.
            from .contracts import MAX_BATCH_BYTES
            if len(canonical(payload).encode())>MAX_BATCH_BYTES:
                raise ValueError('Creation batch exceeds 32 KiB UTF-8.')
            missing=_missing(shape)
            if not missing and not allow_valid:return None
            if not allow_valid:
                # Preserve missing-style-only abstention before target/source checks.
                probe=shape.model_dump()
                probe['styles'] += [{'id':key} for key in missing]
                CreateDocument.model_validate(probe)
        except (ValidationError,TypeError,ValueError):
            if allow_valid:raise
            return None
        _absent(store,shape.path)
        if owner['artifact']!=shape.path:
            raise HarnessError('creation_repair_scope','Creation repair must address the active task artifact.')
        # Resolve sources before classifying design failures. No synthetic styles
        # or resolved text are persisted into the original submitted payload.
        try:
            resolved,bindings=bind_creation_shape(store,task_id,shape)
        except HarnessError as exc:
            if allow_valid or exc.code in ('state_corrupt','chat_scope','creation_repair_scope'):raise
            return None
        except ValidationError:
            if allow_valid:raise
            return None
        probe=resolved.model_dump()
        probe['styles'] += [{'id':key} for key in missing]
        design_errors=[]
        try:
            CreateDocument.model_validate(probe)
        except ValidationError as exc:
            if not allow_valid:return None
            design_errors=[{'path':'.'.join(str(p) for p in item['loc']) or '$',
                            'code':item['type'],'message':item['msg']}
                           for item in exc.errors(include_url=False,include_input=False)]
        raw=json.loads(canonical(payload))
        if previous and previous['payload_hash']==_hash(raw) and previous.get('review_required',False)==allow_valid:
            try:_fresh(store,previous)
            except HarnessError as exc:
                if exc.code not in FRESHNESS_ERRORS:raise
                # This explicit submission already passed current target/source
                # checks above. Issue a new handle; never revive the stale one.
            else:return _summary(previous,store)
        state={'version':1,'ref':'cr_'+uuid.uuid4().hex,'task_id':task_id,'chat_id':owner['chat_id'],
               'objective_hash':digest(owner['objective'].encode()),'artifact':owner['artifact'],
               'sources':_sources(store,task_id),'target_absent':True,'payload':raw,'payload_hash':_hash(raw),
               'missing':missing,'declared':['Normal']+[s.id for s in shape.styles if s.id!='Normal'],
               'literal_bindings':bindings,'resolved_request':None,'resolved_hash':None,'review_required':allow_valid}
        if design_errors:state['design_errors']=design_errors
        _save(store,state)
        return _summary(state,store)


def pending(store,task_id):
    """Expose stale navigation without blocking a fresh explicit proposal.

    Corruption, ownership and path errors remain failures, not stale summaries.
    """
    state=get(store,task_id)
    if state is None:return None
    try:_fresh(store,state)
    except HarnessError as exc:
        if exc.code not in FRESHNESS_ERRORS:raise
        return {'ref':state['ref'],'status':'stale','document_changed':False,
                'new_proposal_needed':True,'reason':exc.code,
                'instruction':'This repair is stale. Recheck current target and sources, then submit a fresh creation proposal if the target is still absent. Do not reuse this repair handle.'}
    return _summary(state,store)


def _binding_slot(payload,slot):
    """Return only an existing declared creation text slot and its fixed role."""
    bands=('header','footer','first_header','first_footer','even_header','even_footer')
    if slot in {name+'.text' for name in bands}:
        name=slot.split('.')[0]
        if isinstance(payload.get(name),dict):return payload[name],'text',name.rsplit('_',1)[-1]
    elif match:=re.fullmatch(r'blocks\.(0|[1-9][0-9]*)\.text',slot):
        index=int(match[1]);blocks=payload['blocks']
        if index<len(blocks) and blocks[index]['kind']=='paragraph':return blocks[index],'text','paragraph'
    elif match:=re.fullmatch(r'blocks\.(0|[1-9][0-9]*)\.rows\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',slot):
        block,row,column=map(int,match.groups());blocks=payload['blocks']
        if block<len(blocks) and blocks[block]['kind']=='table':
            rows=blocks[block]['rows']
            if row<len(rows) and column<len(rows[row]):return rows[row],column,'table_cell'
    raise HarnessError('content_slot','Source binding must address an existing declared paragraph, cell or band text slot.')


def _binding_error(slot,expected,received,reason):
    """Bounded diagnostics only: never infer a replacement or expose other sources."""
    if reason=='invalid_slot':
        code='content_slot'
        message='Invalid source-binding slot; use an existing declared text slot.'
        correction='Use an existing proposal text slot; match its role to an active reference. Do not ask the user for internal IDs.'
    else:
        code='content_reference'
        message=f'Binding {slot}: expected {expected}; received {received or "not active"}.'
        correction='Choose an active reference of the expected role and check its text against this slot. Do not ask the user for internal IDs.'
    raise HarnessError(code,message,recovery={
        'slot':slot,'expected_role':expected,'received_role':received,
        'reason':reason,'correction':correction})


def resolve(store,task_id,ref,styles,source_bindings=None):
    """Add missing styles and only explicitly selected current source bindings."""
    with store.transaction():
        state=get(store,task_id,ref);_fresh(store,state)
        source_bindings={} if source_bindings is None else source_bindings
        if (not isinstance(source_bindings,dict) or len(source_bindings)>24 or
                any(not isinstance(slot,str) or not 1<=len(slot)<=100 or
                    not isinstance(reference,str) or not re.fullmatch(r'ct_[0-9a-f]{24}',reference)
                    for slot,reference in source_bindings.items())):
            raise HarnessError('creation_repair_bindings','Supply at most 24 text-slot/source-reference bindings; slot keys must be 1–100 characters.')
        if not isinstance(styles,list) or not 0<=len(styles)<=16:
            raise HarnessError('creation_repair_styles','Supply at most 16 explicit missing style declarations; empty only when none are missing.')
        declarations=[WordStyle.model_validate(s) for s in styles]
        ids=[s.id for s in declarations]
        if len(ids)!=len(set(ids)) or set(ids)!=set(state['missing']):
            raise HarnessError('creation_repair_styles','Declare exactly the missing IDs once; existing styles cannot be changed.')
        payload=json.loads(canonical(state['payload']))
        payload['styles']=payload.get('styles',[])+[s.model_dump(exclude_unset=True) for s in declarations]
        source_plan=None
        if source_bindings:
            from .content_plan import active_plan,resolve as resolve_literal
            active=active_plan(store,task_id)
            members={item['ref']:item['role'] for item in active['refs']} if active else {}
            for slot,reference in source_bindings.items():
                try:parent,key,role=_binding_slot(payload,slot)
                except HarnessError as exc:
                    if exc.code!='content_slot':raise
                    _binding_error(slot,None,members.get(reference),'invalid_slot')
                if members.get(reference)!=role:
                    _binding_error(slot,role,members.get(reference),
                                   'role_mismatch' if reference in members else 'inactive_reference')
                text=resolve_literal(store,task_id,reference,role=role)
                current=parent[key]
                legacy=payload.get('literal_refs',{}).get(slot)
                # Binding confirms identity; it is not a content replacement.
                # Existing references retain their exact provenance identity.
                compatible=(current=={'ref':reference} if isinstance(current,dict)
                            else current=='' or current==text)
                if legacy is not None and legacy!=reference:compatible=False
                if not compatible:
                    raise HarnessError('content_conflict',
                        f'Binding {slot} conflicts with existing proposal content. Revise that slot explicitly before finalizing.',
                        recovery={'slot':slot,'expected_role':role,'reason':'content_conflict',
                                  'correction':'Bindings confirm matching text or fill empty slots. Use edit_creation_blocks or revise_creation for explicit content replacement, then finalize the new proposal ref.'})
                parent[key]={'ref':reference}
                # Replace only this slot's legacy binding; other bindings remain
                # subject to their original conflict and freshness checks.
                if 'literal_refs' in payload:payload['literal_refs'].pop(slot,None)
            source_plan={'generation':active['generation'],'hash':_hash(active)}
        request=CreateDocument.model_validate(payload)
        _,bindings=bind_creation(store,task_id,request)  # Revalidate full resolved limits; return refs for staging.
        state['resolved_request']=request.model_dump()
        state['resolved_hash']=_hash(state['resolved_request'])
        state['resolved_source_bindings']=[item for item in bindings if item['slot'] in source_bindings]
        state['resolved_source_plan']=source_plan
        _save(store,state)
        return request


