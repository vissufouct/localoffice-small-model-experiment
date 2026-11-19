"""Task-scoped efficiency evidence; unknown usage is never reported as zero tokens."""
import json
import time
import uuid
from .storage import canonical
from .errors import HarnessError
from .chats import current_chat


def begin(store,task_id,kind,**body):
    key=str(uuid.uuid4())
    with store.transaction():
        store.db.execute('INSERT INTO task_measurements VALUES(?,?,?,?,?)',
                         (key,task_id,kind,time.time(),canonical({'status':'running',**body})))
    return key


def finish(store,key,**values):
    with store.transaction():
        row=store.db.execute('SELECT body FROM task_measurements WHERE id=?',(key,)).fetchone()
        body=json.loads(row[0]);body.update(values)
        store.db.execute('UPDATE task_measurements SET body=? WHERE id=?',(canonical(body),key))


def summary(store,task_id):
    if not store.db.execute('SELECT 1 FROM task_chats WHERE task_id=? AND chat_id=?',(task_id,current_chat(store))).fetchone():
        raise HarnessError('chat_scope','Task measurements belong to another chat.')
    rows=[{'kind':r['kind'],**json.loads(r['body'])} for r in store.db.execute('SELECT * FROM task_measurements WHERE task_id=? ORDER BY created',(task_id,))]
    calls=[r for r in rows if r['kind']=='inference']
    complete=bool(calls) and all(isinstance(r.get('usage',{}).get('prompt_tokens'),int) and isinstance(r.get('usage',{}).get('completion_tokens'),int) for r in calls)
    return {'task_id':task_id,'model_calls':len(calls),'tool_calls':sum(r['kind']=='tool' for r in rows),
            'repair_events':sum(r['kind']=='repair' for r in rows),'normalization_events':sum(r['kind']=='normalization' for r in rows),'usage_complete':complete,
            'input_tokens':sum(r['usage']['prompt_tokens'] for r in calls) if complete else None,
            'output_tokens':sum(r['usage']['completion_tokens'] for r in calls) if complete else None,
            'observations':rows}
