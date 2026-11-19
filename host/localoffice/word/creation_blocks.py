"""Explicit typed proposal edits, atomic generic revision and per-call receipts."""
import json
import re
from ..errors import HarnessError
from ..storage import canonical,digest
from .contracts import EditCreationBlocks
from . import creation_repair as repair
from .creation_revision import revise,replay_receipt

PREFIX='word_creation_block_result:'


def _hash(value):
    return digest(canonical(value).encode())


def normalize(arguments):
    """Canonical typed request: tool included, absent optional properties omitted."""
    request=EditCreationBlocks.model_validate(arguments)
    return {'tool':request.tool,'ref':request.ref,
            'changes':[change.model_dump(exclude_none=True) for change in request.changes]}


def _owner(store,task_id):
    owner=repair._owner(store,task_id)
    return {'task_id':task_id,'chat_id':owner['chat_id'],'artifact':owner['artifact']}


def _native_call(store,task_id,call_id,request):
    owner=_owner(store,task_id)
    if not isinstance(call_id,str) or not re.fullmatch(r'call_[0-9a-f]{32}',call_id):
        raise HarnessError('native_history','Expected a host-issued native call identity.')
    row=store.db.execute('SELECT * FROM native_calls WHERE call_id=? AND task_id=? AND chat_id=?',
                         (call_id,task_id,owner['chat_id'])).fetchone()
    if row is None:raise HarnessError('native_history','Block edit call belongs to another task or chat.')
    try:
        function=json.loads(row['function'])
        if function['name']!='word.edit_creation_blocks' or normalize(function['arguments'])!=request:
            raise ValueError('request')
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('native_history','Block edit call has different or invalid arguments.') from exc
    return owner,row


def _receipt(store,owner,call_id,request):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+call_id,)).fetchone()
    if row is None:return None
    try:
        envelope=json.loads(row[0]);state=envelope['state']
        if envelope['hash']!=_hash(state):raise ValueError('envelope')
        if state['version']!=1 or state['call_id']!=call_id:raise ValueError('identity')
        if state['request_hash']!=_hash(state['request']):raise ValueError('request')
        if state['receipt_hash']!=_hash(state['receipt']):raise ValueError('receipt')
    except (ValueError,KeyError,TypeError) as exc:
        raise HarnessError('state_corrupt','Block edit receipt failed its integrity check.') from exc
    if state['owner']!=owner or state['request']!=request:
        raise HarnessError('native_history','Block edit receipt has different ownership or arguments.')
    return state['receipt']


def result(store,task_id,args,*,native_call_id):
    """Exact historical execution evidence only; never resolves sources or edits."""
    request=normalize(args)
    with store.transaction():
        owner,_=_native_call(store,task_id,native_call_id,request)
        return _receipt(store,owner,native_call_id,request)


def edit(store,task_id,*,ref,changes,native_call_id=None):
    request=normalize({'ref':ref,'changes':changes})
    origin={'ref':ref,'request_hash':_hash(request)}
    with store.transaction():
        owner=_owner(store,task_id)
        if native_call_id is not None:
            owner,row=_native_call(store,task_id,native_call_id,request)
            receipt=_receipt(store,owner,native_call_id,request)
            if receipt is not None:return receipt
            if row['result_message'] is not None:
                raise HarnessError('native_history','Completed block edit call has no recorded receipt.')
        state=repair.get(store,task_id)
        if state is None:raise HarnessError('creation_repair_reference','Unknown creation proposal.')
        repair._fresh(store,state)
        if (state.get('block_revision_origin')==origin
                and state.get('block_revision_result_ref')==state['ref']):
            receipt=replay_receipt(store,state)
        else:
            state=repair.get(store,task_id,ref)
            # Every index addresses the immutable proposal named by ref.
            blocks=state['payload']['blocks']
            patches=[]
            removals=set()
            properties=set()
            for change in request['changes']:
                index=change['index']
                if index>=len(blocks):
                    raise HarnessError('creation_revision','Block index is out of range in the referenced proposal.')
                if change['op']=='remove_block':
                    if index in removals:
                        raise HarnessError('creation_revision','Duplicate removal of the same proposal block.')
                    removals.add(index)
                else:
                    if blocks[index].get('kind')!='paragraph':
                        raise HarnessError('creation_revision','set_paragraph requires a paragraph in the referenced proposal.')
                    for key in ('text','style','page_break_before'):
                        if key in change:
                            if (index,key) in properties:
                                raise HarnessError('creation_revision','Duplicate assignment of the same paragraph property.')
                            properties.add((index,key))
                            patches.append({'op':'set','path':f'blocks.{index}.{key}','value':change[key]})
            if any(index in removals for index,key in properties):
                raise HarnessError('creation_revision','Cannot remove and set the same proposal block.')
            # Generic revisions remain sequential: sets keep original indices,
            # then descending removals cannot shift any remaining target.
            patches.extend({'op':'remove','path':f'blocks.{index}','value':None}
                           for index in sorted(removals,reverse=True))
            receipt=revise(store,task_id,ref,patches)
            revised=repair.get(store,task_id,receipt['ref'])
            revised['block_revision_origin']=origin
            revised['block_revision_result_ref']=revised['ref']
            repair._save(store,revised)
        if native_call_id is not None:
            saved={'version':1,'owner':owner,'call_id':native_call_id,
                   'request':request,'request_hash':_hash(request),
                   'receipt':receipt,'receipt_hash':_hash(receipt)}
            store.db.execute('INSERT INTO meta VALUES(?,?)',
                (PREFIX+native_call_id,canonical({'state':saved,'hash':_hash(saved)})))
        return receipt
