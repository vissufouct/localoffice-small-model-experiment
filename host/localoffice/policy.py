"""Per-task consent is separate from available tools and external-data permission."""
from .errors import HarnessError


def task_policy(store, task_id):
    from .chats import current_chat
    task=store.db.execute('SELECT 1 FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE t.id=? AND c.chat_id=?',(task_id,current_chat(store))).fetchone()
    if not task:raise HarnessError('not_found','Task not found in this chat.')
    row=store.db.execute('SELECT online_research FROM task_policy WHERE task_id=?',(task_id,)).fetchone()
    return {'version':1,'online_research_opt_in':bool(row and row[0]),
            'research_connector_available':False,'effective_network':'local_only',
            'external_ai_allowed':False,'project_uploads_allowed':False}


def bind_policy(store,task_id,opt_in,resuming):
    existing=store.db.execute('SELECT online_research FROM task_policy WHERE task_id=?',(task_id,)).fetchone()
    allowed=bool(existing and existing[0]) if resuming else bool(opt_in)
    if resuming and opt_in is not None and bool(opt_in)!=allowed:
        raise HarnessError('task_policy','A resumed task retains its research choice. Start a new task to change it.')
    store.db.execute('INSERT OR IGNORE INTO task_policy VALUES(?,?)',(task_id,int(allowed)))
