"""Explicit bounded revisions of retained creation proposals; no document writes."""
import copy
import math
import re
from ..errors import HarnessError
from ..storage import canonical,digest
from . import creation_repair as repair

ROOTS=frozenset(('styles','page','blocks','header','footer','first_header','first_footer',
                 'even_header','even_footer','literal_refs'))
INDEX=re.compile(r'(0|[1-9][0-9]*)\Z')
REF=re.compile(r'cr_[0-9a-f]{32}\Z')


def _fail(message):
    raise HarnessError('creation_revision',message)


def _json(value,depth=0):
    if depth>32:_fail('Revision JSON nesting exceeds 32 levels.')
    if value is None or type(value) is bool or type(value) is int:return
    if type(value) is float:
        if not math.isfinite(value):_fail('Revision values must be finite JSON.')
        return
    if isinstance(value,str):
        if any(0xD800<=ord(c)<=0xDFFF for c in value):_fail('Revision strings must be valid Unicode.')
        return
    if isinstance(value,list):
        for item in value:_json(item,depth+1)
        return
    if isinstance(value,dict) and all(isinstance(key,str) for key in value):
        for key,item in value.items():_json(key,depth+1);_json(item,depth+1)
        return
    _fail('Revision values must be JSON primitives, arrays or objects.')


def _paths(changes):
    if not isinstance(changes,list) or not 1<=len(changes)<=24:_fail('Supply 1–24 proposal changes.')
    _json(changes)
    try:size=len(canonical(changes).encode())
    except (ValueError,OverflowError,RecursionError) as exc:
        raise HarnessError('creation_revision','Invalid revision JSON.') from exc
    if size>32768:_fail('Proposal changes exceed 32 KiB UTF-8.')
    result=[]
    for change in changes:
        if not isinstance(change,dict):_fail('Each change must be an object.')
        op=change.get('op')
        if op not in ('set','remove','insert'):_fail('Unknown proposal change operation.')
        required={'op','path'} | ({'value'} if op!='remove' else set())
        if op=='remove':
            if not required<=set(change) or set(change)-required-{'value'} or change.get('value') is not None:
                _fail('Remove permits only an omitted or null value.')
        elif set(change)!=required:_fail('Set/insert require value. No extra change fields.')
        path=change['path']
        if not isinstance(path,str) or not 1<=len(path)<=100:_fail('Change paths must be 1–100 characters.')
        parts=path.split('.')
        if parts[0] not in ROOTS:
            _fail('Summary fields and target identity are read-only. Use word.revise_creation with content/design roots: blocks, styles, page, header, footer, first_header, first_footer, even_header, even_footer or literal_refs. For paragraph text/style/page_break_before or block removal, load word.edit_creation_blocks; use original proposal indices. Correct the tool call; no user confirmation is needed for an internal path error.')
        if any(not part or part.startswith('-') or
                (part.isdecimal() and not INDEX.fullmatch(part)) for part in parts):
            _fail('Invalid or protected proposal path.')
        result.append((change,parts))
    return result


def _index(segment,length,insert=False):
    if not INDEX.fullmatch(segment):_fail('Array indexes must be canonical nonnegative integers.')
    index=int(segment)
    if index>length or (index==length and not insert):_fail('Proposal array index is out of range.')
    return index


def _apply(payload,change,parts,*,batch_intermediate=False):
    try:
        _apply_change(payload,change,parts)
    except HarnessError as exc:
        # Enrich only a verified missing top-level style declaration. Preserve
        # syntax, ownership, freshness and all other traversal error precedence.
        missing='styles' not in payload
        styles=payload.get('styles',[])
        eligible=(exc.code=='creation_revision' and parts[0]=='styles' and len(parts)>1
                  and INDEX.fullmatch(parts[1]) and isinstance(styles,list))
        out_of_range=(eligible and not missing and int(parts[1])>=len(styles)
                      and str(exc)=='Proposal array index is out of range.')
        absent=(eligible and missing and str(exc)=='Change ancestor does not exist; set it explicitly first.')
        if not (out_of_range or absent):raise
        count=len(styles)
        location=' at failing change (batch-intermediate)' if batch_intermediate else ''
        message=f'{change["path"]}: {count} explicit styles{location}.'
        if count==0:message+=' Implicit Normal is not indexed.'
        if missing:
            message+=' styles is absent: set styles declares the list; insert requires that list.'
        else:
            message+=f' set styles replaces the list; insert styles.{count} appends a declaration.'
        message+=' Preserve existing entries. Batch rejected; no changes applied.'
        _fail(message)


def _apply_change(payload,change,parts):
    parent=payload
    for part in parts[:-1]:
        if isinstance(parent,dict):
            if part not in parent:_fail('Change ancestor does not exist; set it explicitly first.')
            parent=parent[part]
        elif isinstance(parent,list):parent=parent[_index(part,len(parent))]
        else:_fail('Change ancestor is not an object or array.')
    key=parts[-1];op=change['op']
    if isinstance(parent,dict):
        if op=='insert':_fail('Insert requires an array position.')
        if op=='remove':
            if key not in parent:_fail('Removed proposal field does not exist.')
            del parent[key]
        else:parent[key]=copy.deepcopy(change['value'])
    elif isinstance(parent,list):
        index=_index(key,len(parent),op=='insert')
        if op=='insert':parent.insert(index,copy.deepcopy(change['value']))
        elif op=='remove':parent.pop(index)
        else:parent[index]=copy.deepcopy(change['value'])
    else:_fail('Change parent is not an object or array.')


def replay_receipt(store,state):
    """Original revision receipt; older records retain their previous fallback."""
    saved=state.get('revision_receipt')
    if saved is None:return repair.pending(store,state['task_id'])
    if (state.get('revision_receipt_hash')!=digest(canonical(saved).encode())
            or saved.get('ref')!=state['ref']):
        raise HarnessError('state_corrupt','Revision receipt failed its integrity check.')
    return copy.deepcopy(saved)


def result(store,task_id,ref,changes):
    """Exact current/fresh revision result; no execution or inferred new call."""
    _paths(changes)
    request_hash=digest(canonical({'ref':ref,'changes':changes}).encode())
    with store.transaction():
        state=repair.get(store,task_id)
        if state is None:return None
        origin=state.get('revision_origin',{})
        if origin.get('ref')!=ref or origin.get('request_hash')!=request_hash:return None
        repair._fresh(store,state)
        return replay_receipt(store,state)


def revise(store,task_id,ref,changes):
    """Revise only an explicitly addressed retained payload, atomically.

    Exact replay is allowed only while its resulting proposal remains current
    and fresh. No historical payload is revived and no source plan is changed.
    """
    paths=_paths(changes)
    if not isinstance(ref,str) or not REF.fullmatch(ref):_fail('Invalid proposal reference.')
    request_hash=digest(canonical({'ref':ref,'changes':changes}).encode())
    with store.transaction():
        state=repair.get(store,task_id)  # Verifies original envelope before replay comparisons.
        if state is None:raise HarnessError('creation_repair_reference','Unknown creation proposal.')
        origin=state.get('revision_origin')
        if origin and origin.get('ref')==ref and origin.get('request_hash')==request_hash:
            repair._fresh(store,state)
            return replay_receipt(store,state)
        state=repair.get(store,task_id,ref)
        repair._fresh(store,state)
        payload=copy.deepcopy(state['payload'])
        for index,(change,parts) in enumerate(paths):
            _apply(payload,change,parts,batch_intermediate=index>0)
        # capture validates shape, hard guards and sources; review may retain
        # remaining design errors explicitly, never as executable readiness.
        # The surrounding transaction rolls back both capture and provenance.
        result=repair.capture(store,task_id,payload,allow_valid=True)
        revised=repair.get(store,task_id,result['ref'])
        if revised['ref']==ref:
            # Even a value-preserving explicit revision gets a new identity;
            # otherwise a different edit could reuse the superseded old handle.
            import uuid
            revised['ref']='cr_'+uuid.uuid4().hex
            revised['resolved_request']=None
            revised['resolved_hash']=None
            revised.pop('resolved_source_bindings',None)
            revised.pop('resolved_source_plan',None)
        revised['revision_origin']={'ref':ref,'request_hash':request_hash,'payload_hash':state['payload_hash']}
        from .creation_effect import summarize
        revised['edit_effect']=summarize(state['payload'],revised['payload'],changes)
        revised['edit_effect_current']=True
        repair._save(store,revised)
        receipt=repair.pending(store,task_id)
        revised['revision_receipt']=receipt
        revised['revision_receipt_hash']=digest(canonical(receipt).encode())
        repair._save(store,revised)
        return receipt
