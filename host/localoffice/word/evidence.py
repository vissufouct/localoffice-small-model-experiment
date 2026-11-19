"""Short read-evidence references; exact hashes remain host-side preconditions."""
import copy
import uuid
from ..chats import current_chat
from ..errors import HarnessError


def owner(store,task_id):
    row=store.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone() if task_id else None
    if not row or row[0]!=current_chat(store):raise HarnessError('evidence_scope','Evidence references require their original task and chat.')


def issue(store,task_id,result):
    if not task_id:return result
    owner(store,task_id)
    result=copy.deepcopy(result)
    with store.transaction():
        count=store.db.execute('SELECT count(*) FROM word_evidence WHERE task_id=?',(task_id,)).fetchone()[0]
        for obj in result.get('objects',result.get('matches',[])):
            identity=(task_id,result['path'],result['revision'],obj['id'],obj['hash'])
            row=store.db.execute('SELECT ref FROM word_evidence WHERE task_id=? AND path=? AND revision=? AND object_id=? AND object_hash=?',identity).fetchone()
            if row:ref=row[0]
            elif count<4096:
                ref='ev_'+uuid.uuid4().hex[:16]
                store.db.execute('INSERT INTO word_evidence VALUES(?,?,?,?,?,?)',(ref,*identity));count+=1
            else:continue  # Keep full-hash evidence usable; never evict replay facts.
            obj['evidence_ref']=ref
        from .read_context import remember
        remember(store,task_id,result)
    return result


def resolve(store,task_id,path,revision,object_id,ref):
    owner(store,task_id)
    row=store.db.execute('SELECT object_hash FROM word_evidence WHERE ref=? AND task_id=? AND path=? AND revision=? AND object_id=?',
                         (ref,task_id,path,revision,object_id)).fetchone()
    if not row:raise HarnessError('stale_evidence','Read the target again; the evidence reference does not match this task, path, revision and object.')
    return row[0]


def model_receipt(value):
    """Keep full hashes in logs/graph; avoid asking the model to copy them."""
    from .content_plan import model_receipt as literal_receipt
    value=literal_receipt(value)
    if not isinstance(value,dict) or not ('revision' in value and 'path' in value):return value
    result=copy.deepcopy(value)
    for obj in result.get('objects',result.get('matches',[])):
        if 'evidence_ref' in obj:obj['hash']=obj.pop('evidence_ref')
    return result
