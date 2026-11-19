"""Raw native preparation presence, separate from normalized proposal identity."""
import json
import re
from ..errors import HarnessError
from ..storage import canonical,digest
from .contracts import PrepareCreation
from . import creation_repair as repair

PREFIX='word_creation_submission:'


def _hash(value):return digest(canonical(value).encode())


def _owner(store,task_id):
    owner=repair._owner(store,task_id)
    return {'task_id':task_id,'chat_id':owner['chat_id'],'artifact':owner['artifact']}


def _call(store,task_id,call_id):
    owner=_owner(store,task_id)
    if not isinstance(call_id,str) or not re.fullmatch(r'call_[0-9a-f]{32}',call_id):
        raise HarnessError('native_history','Expected a host-issued preparation call identity.')
    row=store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                         (call_id,task_id,owner['chat_id'])).fetchone()
    if row is None:raise HarnessError('native_history','Preparation call belongs to another task or chat.')
    try:
        function=json.loads(row['function'])
        if function['name']!='word.prepare_creation':raise ValueError('tool')
        raw=function['arguments']
        request=PrepareCreation.model_validate(raw)
        payload=request.model_dump();payload['tool']='word.create'
        if request.path!=owner['artifact']:raise ValueError('target')
    except (ValueError,TypeError,KeyError) as exc:
        raise HarnessError('native_history','Preparation call has invalid arguments or target.') from exc
    return owner,row,raw,payload


def _paths(raw):
    paths=[]
    def visit(value,prefix=''):
        if isinstance(value,dict):
            for key,item in value.items():
                path=f'{prefix}.{key}' if prefix else key
                paths.append(path);visit(item,path)
        elif isinstance(value,list):
            for index,item in enumerate(value):visit(item,f'{prefix}.{index}')
    visit(raw)
    return sorted(paths)


def verify(store,task_id,payload,*,native_call_id=None):
    owner=_owner(store,task_id)
    if native_call_id is None:return {'status':'unknown'}
    owner,row,raw,normalized=_call(store,task_id,native_call_id)
    if row['result_message'] is not None:
        raise HarnessError('native_history','Preparation call is already completed.')
    if canonical(normalized)!=canonical(payload):
        raise HarnessError('native_history','Preparation arguments differ from the normalized payload.')
    paths=_paths(raw)
    if len(canonical(paths).encode())>32768:
        raise HarnessError('creation_submission','Provided-field provenance exceeds 32 KiB.')
    return {'status':'verified','native_call_id':native_call_id,'owner':owner,
            'raw_arguments_hash':_hash(raw),'payload_hash':_hash(payload),'provided_fields':paths}


def _read(store,task_id,call_id):
    owner,_,raw,payload=_call(store,task_id,call_id)
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+call_id,)).fetchone()
    if row is None:return None
    try:
        envelope=json.loads(row[0]);saved=envelope['state'];verified=saved['verified']
        if envelope['hash']!=_hash(saved) or saved['version']!=1:raise ValueError('envelope')
        if verified['owner']!=owner or verified['native_call_id']!=call_id:raise ValueError('owner')
        if verified['raw_arguments_hash']!=_hash(raw) or verified['payload_hash']!=_hash(payload):raise ValueError('arguments')
        if verified['provided_fields']!=_paths(raw):raise ValueError('paths')
        if saved['receipt_hash']!=_hash(saved['receipt']):raise ValueError('receipt')
        if saved['receipt']['ref']!=saved['proposal_ref']:raise ValueError('ref')
    except (ValueError,TypeError,KeyError) as exc:
        raise HarnessError('state_corrupt','Creation submission failed its integrity check.') from exc
    return saved


def _save(store,call_id,saved):
    store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
        (PREFIX+call_id,canonical({'state':saved,'hash':_hash(saved)})))


def presence(store,state):
    """Verified current initial-submission paths; older/revised records are unknown."""
    _owner(store,state['task_id'])
    marker=state.get('submission_presence')
    if not marker or marker.get('status')=='unknown':return None
    if marker.get('proposal_ref')!=state['ref'] or marker.get('payload_hash')!=state['payload_hash']:
        return None
    if state.get('revision_origin') or state.get('block_revision_origin'):return None
    saved=_read(store,state['task_id'],marker.get('native_call_id'))
    if saved is None:raise HarnessError('state_corrupt','Creation submission association is missing.')
    expected={key:saved['verified'][key] for key in ('status','native_call_id','raw_arguments_hash','payload_hash')}
    expected['proposal_ref']=saved['proposal_ref']
    if marker!=expected:raise HarnessError('state_corrupt','Creation submission marker differs from its association.')
    return set(saved['verified']['provided_fields'])


def record(store,task_id,proposal_ref,verified,receipt):
    """Atomically install presence and freeze the resulting exact summary receipt."""
    with store.transaction():
        state=repair.get(store,task_id,proposal_ref)
        if verified.get('status')=='unknown':
            state['submission_presence']={'status':'unknown'}
            repair._save(store,state)
            return state['submission_presence']
        call_id=verified['native_call_id']
        if verify(store,task_id,state['payload'],native_call_id=call_id)!=verified:
            raise HarnessError('native_history','Preparation verification changed before capture.')
        if _read(store,task_id,call_id) is not None:
            raise HarnessError('native_history','Preparation association is immutable.')
        marker={key:verified[key] for key in ('status','native_call_id','raw_arguments_hash','payload_hash')}
        marker['proposal_ref']=proposal_ref
        saved={'version':1,'verified':verified,'proposal_ref':proposal_ref,
               'receipt':receipt,'receipt_hash':_hash(receipt)}
        _save(store,call_id,saved)
        state['submission_presence']=marker
        repair._save(store,state)
        saved['receipt']=repair.pending(store,task_id)
        saved['receipt_hash']=_hash(saved['receipt'])
        _save(store,call_id,saved)
        return marker


def result(store,task_id,*,native_call_id):
    """Historical per-call receipt, not evidence of current source freshness."""
    with store.transaction():
        saved=_read(store,task_id,native_call_id)
        return saved['receipt'] if saved else None
