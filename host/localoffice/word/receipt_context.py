"""Lossless deduplication against current, exact native creation ledger evidence."""
import copy
import json
from ..storage import canonical,digest
from ..errors import HarnessError


def project(store,task_id,chat_id,message_id,receipt,progress,path,revision):
    if not isinstance(receipt,dict) or receipt.get('status')!='validated':return receipt
    evidence=receipt.get('creation_verification')
    if not isinstance(evidence,dict) or receipt.get('candidate_revision')!=revision:return receipt
    from ..native_history import _owner
    if _owner(store,task_id)!=chat_id:return receipt
    candidate=store.candidate(path)
    current_revision=candidate['candidate_hash'] if candidate else store.file_hash(path)
    if current_revision!=revision:return receipt
    current=next((item for item in (progress or {}).get('receipts',[])
                  if item.get('receipt_id')==receipt.get('receipt_id')
                  and item.get('matches_current_revision') is True),None)
    if current is None or not isinstance(current.get('creation_verification'),dict):return receipt
    call=store.db.execute('SELECT * FROM native_calls WHERE result_message=? AND task_id=? AND chat_id=?',
                          (message_id,task_id,chat_id)).fetchone()
    if call is None:return receipt
    if json.loads(call['function']).get('name') not in ('word.create','word.finalize_creation','word.repair_creation'):return receipt
    operation=store.db.execute('SELECT * FROM operations WHERE id=? AND task_id=? AND path=? AND candidate_hash=? AND idempotency_key=?',
                               (receipt['receipt_id'],task_id,path,revision,'native:'+call['call_id'])).fetchone()
    if operation is None or operation['status']!='validated' or operation['tool']!='word.stage':return receipt
    if digest(canonical(json.loads(operation['args'])).encode())!=operation['args_hash']:
        raise HarnessError('state_corrupt','Creation receipt arguments failed their integrity check.')
    from .plugin import WordPlugin
    if canonical(WordPlugin(store)._receipt(operation))!=canonical(receipt):return receipt
    result=copy.deepcopy(receipt);reduced=result['creation_verification'];represented=current['creation_verification']
    changed=False
    for key,value in list(reduced.items()):
        if key in ('omitted','scope'):continue
        other=represented.get(key)
        if isinstance(value,list) and isinstance(other,list):
            known={canonical(item) for item in other}
            remaining=[item for item in value if canonical(item) not in known]
            if len(remaining)!=len(value):
                changed=True
                if remaining:reduced[key]=remaining
                else:del reduced[key]
        elif key in represented and canonical(value)==canonical(other):
            del reduced[key];changed=True
    if changed:reduced['repeated_facts_in_current_state']=True
    return result
