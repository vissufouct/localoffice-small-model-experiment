"""Durable task recovery facts, reconciled only with exact execution evidence."""
import json
from .errors import HarnessError
from .storage import canonical

PREFIX='task_recovery:'


def load(store,task_id,chat_id,*,reconcile=True):
    owner=store.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone()
    if not owner or owner[0]!=chat_id:raise HarnessError('chat_scope','Recovery belongs to another chat.')
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+task_id,)).fetchone()
    if not row:return None
    try:
        state=json.loads(row[0])
        if state['version']!=1 or type(state['unresolved']) is not bool or type(state['mutation']) is not bool or type(state['ledger_cursor']) is not int or state['ledger_cursor']<0 or type(state['message_id']) is not int:
            raise ValueError('Invalid recovery state')
    except (ValueError,KeyError,TypeError):raise HarnessError('state_corrupt','Task recovery state is invalid; preserve it for review.')
    if not reconcile or not state['unresolved']:return state
    task=store.db.execute('SELECT artifact FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task or not task[0]:return state
    # A crash can occur after ledger validation and before the success message.
    # Never infer success from prose, another task, or a noncurrent candidate.
    receipt=store.db.execute("""SELECT o.id,o.path,o.candidate_hash FROM events e JOIN operations o ON o.id=e.operation_id
        WHERE o.task_id=? AND e.seq>? AND e.state='validated'
        AND o.tool IN ('word.stage','markdown.stage') AND o.status IN ('validated','committed')
        AND NOT EXISTS (SELECT 1 FROM events prior WHERE prior.operation_id=o.id AND prior.state='validated' AND prior.seq<=?)
        ORDER BY e.seq DESC LIMIT 1""",(task_id,state['ledger_cursor'],state['ledger_cursor'])).fetchone()
    if not receipt or receipt['path']!=task[0]:return state
    candidate=store.candidate(task[0]);current=candidate['candidate_hash'] if candidate else store.file_hash(task[0])
    if current!=receipt['candidate_hash']:return state
    if candidate:
        try:store.blob(current)  # Verify bytes, not just the saved hash label.
        except OSError as exc:raise HarnessError('state_corrupt','Recovery candidate bytes are unavailable.') from exc
    state={**state,'unresolved':False,'mutation':False,'resolved_by':receipt['id']}
    with store.transaction():store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(PREFIX+task_id,canonical(state)))
    return state


def prompt_state(state):
    if not state or not state['unresolved']:return None
    return {'unresolved':True,'rejected_mutation':state['mutation'],'failure_message_id':state['message_id'],
            'instruction':'A previous failure is unresolved. Reads/help do not confirm a rejected edit. Use current evidence to correct it, or ask for review; do not claim completion or replay a validated edit.'}
