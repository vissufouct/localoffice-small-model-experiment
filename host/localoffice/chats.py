"""Project-shared artifacts with explicitly separated conversation scopes."""
import time
import uuid
from .errors import HarnessError

SCHEMA='''
CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_chats(task_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS message_chats(message_id INTEGER PRIMARY KEY,chat_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotation_chats(annotation_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_scopes(decision_id TEXT PRIMARY KEY,scope TEXT NOT NULL,chat_id TEXT);
CREATE TABLE IF NOT EXISTS operation_chats(operation_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_by_chat ON message_chats(chat_id,message_id);
CREATE INDEX IF NOT EXISTS tasks_by_chat ON task_chats(chat_id,task_id);
CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,path TEXT NOT NULL,original_name TEXT NOT NULL,content_hash TEXT NOT NULL,size INTEGER NOT NULL,created REAL NOT NULL);
'''

def initialize(store):
    store.db.executescript(SCHEMA)
    with store.transaction():
        row=store.db.execute('SELECT id FROM chats ORDER BY created LIMIT 1').fetchone()
        if not row:
            key=str(uuid.uuid4());now=time.time()
            store.db.execute('INSERT INTO chats VALUES(?,?,?,?)',(key,'First chat',now,now))
        else:key=row[0]
        store.db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('active_chat',key))
        # Preserve pre-chat project data in one explicit legacy conversation.
        for table,column,source in [('task_chats','task_id','tasks'),('message_chats','message_id','messages'),('annotation_chats','annotation_id','annotations'),('operation_chats','operation_id','operations')]:
            store.db.execute(f'INSERT OR IGNORE INTO {table}({column},chat_id) SELECT id,? FROM {source}',(key,))
        store.db.execute("INSERT OR IGNORE INTO decision_scopes SELECT id,'project',NULL FROM decisions")

def current_chat(store):
    return store.db.execute("SELECT value FROM meta WHERE key='active_chat'").fetchone()[0]

def create_chat(store,title='New chat'):
    title=title.strip()
    if not title or len(title)>120:raise HarnessError('chat_title','Use a chat title from 1 to 120 characters.')
    key=str(uuid.uuid4());now=time.time()
    with store.transaction():store.db.execute('INSERT INTO chats VALUES(?,?,?,?)',(key,title,now,now))
    return key

def select_chat(store,key):
    if not store.db.execute('SELECT 1 FROM chats WHERE id=?',(key,)).fetchone():
        raise HarnessError('not_found','Chat not found in this project.')
    with store.transaction():store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('active_chat',key))

def list_chats(store):
    return [dict(row) for row in store.db.execute('SELECT * FROM chats ORDER BY updated DESC')]

def decisions(store,chat_id):
    return [dict(row) for row in store.db.execute("SELECT d.* FROM decisions d JOIN decision_scopes s ON s.decision_id=d.id WHERE d.active=1 AND (s.scope='project' OR s.chat_id=?)",(chat_id,))]

def owner(store,operation_id):
    row=store.db.execute('SELECT c.id,c.title FROM operation_chats o JOIN chats c ON c.id=o.chat_id WHERE o.operation_id=?',(operation_id,)).fetchone()
    return dict(row) if row else None


def set_active_task(store,key,chat_id=None):
    chat_id=chat_id or current_chat(store)
    if key and not store.db.execute('SELECT 1 FROM task_chats WHERE task_id=? AND chat_id=?',(key,chat_id)).fetchone():
        raise HarnessError('chat_scope','Task belongs to another chat.')
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('active_task:'+chat_id,key or ''))


def active_task(store,chat_id=None):
    chat_id=chat_id or current_chat(store)
    value=store.db.execute('SELECT value FROM meta WHERE key=?',('active_task:'+chat_id,)).fetchone()
    if value:
        if not value[0]:return None
        row=store.db.execute("SELECT t.id FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE t.id=? AND c.chat_id=? AND t.status IN ('running','paused','failed')",(value[0],chat_id)).fetchone()
    else:
        # One-time compatibility fallback for projects created before explicit focus.
        row=store.db.execute("SELECT t.id FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE c.chat_id=? AND t.status IN ('running','paused','failed') ORDER BY t.updated DESC LIMIT 1",(chat_id,)).fetchone()
    return row[0] if row else None
