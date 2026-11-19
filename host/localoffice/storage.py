from __future__ import annotations
import hashlib
import fcntl
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .errors import HarnessError
from .paths import project_path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def atomic_write(path: Path, data: bytes):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS word_content_refs(ref TEXT PRIMARY KEY,task_id TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS image_assets(hash TEXT PRIMARY KEY,metadata TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS image_asset_sources(hash TEXT NOT NULL,path TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(hash,path));
CREATE TABLE IF NOT EXISTS render_cache(key TEXT PRIMARY KEY,revision TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_measurements(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,kind TEXT NOT NULL,created REAL NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_versions(id TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,source_message INTEGER NOT NULL,scope TEXT NOT NULL,chat_id TEXT,created REAL NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS task_skills(task_id TEXT PRIMARY KEY,skill_id TEXT NOT NULL,source_hash TEXT NOT NULL,name TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visual_reviews(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,path TEXT NOT NULL,revision TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS word_evidence(ref TEXT PRIMARY KEY,task_id TEXT NOT NULL,path TEXT NOT NULL,revision TEXT NOT NULL,object_id TEXT NOT NULL,object_hash TEXT NOT NULL,UNIQUE(task_id,path,revision,object_id,object_hash));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,task_id TEXT,role TEXT NOT NULL,content TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS native_calls(call_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,chat_id TEXT NOT NULL,assistant_message INTEGER NOT NULL UNIQUE,function TEXT NOT NULL,result_message INTEGER UNIQUE);
CREATE TRIGGER IF NOT EXISTS native_message_delete AFTER DELETE ON messages BEGIN
 DELETE FROM native_calls WHERE assistant_message=old.id OR result_message=old.id;
END;
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(content,content='messages',content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_insert AFTER INSERT ON messages BEGIN
 INSERT INTO history_fts(rowid,content) VALUES(new.id,new.content); END;
CREATE TRIGGER IF NOT EXISTS messages_delete AFTER DELETE ON messages BEGIN
 INSERT INTO history_fts(history_fts,rowid,content) VALUES('delete',old.id,old.content); END;
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,objective TEXT NOT NULL,status TEXT NOT NULL,mode TEXT NOT NULL,artifact TEXT,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_policy(task_id TEXT PRIMARY KEY,online_research INTEGER NOT NULL DEFAULT 0 CHECK(online_research IN (0,1)));
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,content TEXT NOT NULL,source_message INTEGER NOT NULL,version INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,args_hash TEXT NOT NULL,args TEXT NOT NULL,task_id TEXT,tool TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,path TEXT NOT NULL,input_hash TEXT,candidate_hash TEXT,backup_hash TEXT,linked_operation TEXT,error TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,state TEXT NOT NULL,detail TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(path TEXT PRIMARY KEY,operation_id TEXT NOT NULL,base_hash TEXT,candidate_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(generation INTEGER PRIMARY KEY,task_id TEXT,body TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS annotations(id TEXT PRIMARY KEY,path TEXT NOT NULL,revision TEXT NOT NULL,scope TEXT NOT NULL,locator TEXT NOT NULL,comment TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_annotations(task_id TEXT NOT NULL,annotation_id TEXT NOT NULL,PRIMARY KEY(task_id,annotation_id));
CREATE TABLE IF NOT EXISTS task_scopes(task_id TEXT PRIMARY KEY,path TEXT NOT NULL,base_revision TEXT NOT NULL,head_revision TEXT NOT NULL,ranges TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,kind TEXT NOT NULL,revision TEXT,body TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edges(source TEXT NOT NULL,target TEXT NOT NULL,relation TEXT NOT NULL,provenance TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(source,target,relation));
CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
'''


class ProjectStore:
    def __init__(self, folder: str | Path):
        self.root = Path(folder).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise HarnessError('invalid_project', 'Choose a directory.')
        self.state = self.root / '.localoffice'
        if self.state.is_symlink():
            raise HarnessError('path_denied', 'Project state must not be a symbolic link.')
        self.state.mkdir(mode=0o700, exist_ok=True)
        lock_path = self.state / 'writer.lock'
        if lock_path.is_symlink():
            raise HarnessError('path_denied', 'Project lock cannot be a symbolic link.')
        self.writer_lock = lock_path.open('a+b')
        try:
            fcntl.flock(self.writer_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.writer_lock.close()
            raise HarnessError('project_in_use', 'This project is already open in another harness host. Close it there first.')
        self.blobs = self.state / 'blobs'
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        if self.blobs.is_symlink() or (self.state / 'state.sqlite3').is_symlink():
            raise HarnessError('path_denied', 'State files cannot be symbolic links.')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.state / 'state.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('project_id', str(uuid.uuid4())))
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema_version', '1'))
        self.id = self.db.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()[0]
        from .chats import initialize
        initialize(self)
        from .memory import finish_forget
        finish_forget(self)
        self.recover()

    @contextmanager
    def transaction(self):
        with self.lock:
            # Retain the outer connection transaction contract, including any
            # pending legacy writes. Nested helpers must never commit it early.
            if not getattr(self, '_transaction_depth', 0):
                with self.db:
                    if not self.db.in_transaction:
                        self.db.execute('BEGIN')
                    self._transaction_depth = 1
                    try:
                        yield
                    finally:
                        self._transaction_depth = 0
            else:
                savepoint = 'localoffice_' + uuid.uuid4().hex
                self.db.execute('SAVEPOINT ' + savepoint)
                try:
                    yield
                except BaseException:
                    self.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                    self.db.execute('RELEASE SAVEPOINT ' + savepoint)
                    raise
                else:
                    self.db.execute('RELEASE SAVEPOINT ' + savepoint)

    def close(self):
        self.db.close()
        if not self.writer_lock.closed:
            fcntl.flock(self.writer_lock.fileno(), fcntl.LOCK_UN)
            self.writer_lock.close()

    def put_blob(self, data: bytes) -> str:
        key = digest(data)
        path = self.blobs / key
        if path.is_symlink():
            raise HarnessError('state_corrupt', 'Invalid blob path.')
        if path.exists():
            if digest(path.read_bytes()) != key:
                raise HarnessError('state_corrupt', 'Stored blob failed its hash check.')
        else:
            atomic_write(path, data)
        return key

    def blob(self, key: str) -> bytes:
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise HarnessError('state_corrupt', 'Invalid blob identity.')
        path = self.blobs / key
        if path.is_symlink():
            raise HarnessError('state_corrupt', 'Invalid blob path.')
        data = path.read_bytes()
        if digest(data) != key:
            raise HarnessError('state_corrupt', 'Stored blob failed its hash check.')
        return data

    def file_hash(self, path: str):
        p = project_path(self.root, path)
        if not p.is_file():return None
        with p.open('rb') as source:
            return hashlib.file_digest(source,'sha256').hexdigest()

    def event(self, operation_id: str, state: str, detail=None):
        self.db.execute('INSERT INTO events(operation_id,state,detail,created) VALUES(?,?,?,?)',
                        (operation_id, state, canonical(detail or {}), time.time()))
        self.db.execute('UPDATE operations SET status=? WHERE id=?', (state, operation_id))
        self.node('receipt:' + operation_id, 'receipt', {'operation': operation_id, 'state': state})

    def node(self, key: str, kind: str, body, revision=None):
        self.db.execute('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?)',
                        (key, kind, revision, canonical(body), time.time()))

    def edge(self, source, target, relation, provenance):
        self.db.execute('INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?)',
                        (source, target, relation, provenance, time.time()))

    def operation(self, key: str) -> dict:
        row = self.db.execute('SELECT * FROM operations WHERE id=?', (key,)).fetchone()
        if not row:
            raise HarnessError('not_found', 'Operation not found in this project.')
        return dict(row)

    def candidate(self, path: str):
        row = self.db.execute('SELECT * FROM candidates WHERE path=?', (path,)).fetchone()
        return dict(row) if row else None

    def read(self, path: str, *, working=True, limit=200_000):
        project_path(self.root, path)
        candidate = self.candidate(path) if working else None
        if candidate:
            data = self.blob(candidate['candidate_hash'])
        else:
            p = project_path(self.root, path, must_exist=True)
            if p.stat().st_size > limit:
                raise HarnessError('file_limit', 'Read a bounded source excerpt instead.')
            data = p.read_bytes()
        if len(data) > limit:
            raise HarnessError('file_limit', 'File exceeds this tool limit.')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise HarnessError('format_unsupported', 'Use the appropriate binary-file plugin.')
        return {'path': path, 'text': text, 'revision': digest(data), 'working': bool(candidate)}

    def stage(self, *, path: str, text: str, expected_revision: str | None,
              idempotency_key: str, task_id=None, linked_operation=None) -> dict:
        if not path.endswith('.md') or '\x00' in text or len(text.encode()) > 200_000:
            raise HarnessError('invalid_patch', 'Markdown text must be UTF-8, under 200 KB, without NUL.')
        args = dict(path=path, text=text, expected_revision=expected_revision,
                    task_id=task_id, linked_operation=linked_operation)
        return self._stage_validated_bytes(path=path,data=text.encode(),expected_revision=expected_revision,
                    idempotency_key=idempotency_key,task_id=task_id,linked_operation=linked_operation,
                    tool='markdown.stage',args=args,validation={'utf8':True})

    def _stage_validated_bytes(self, *, path, data, expected_revision, idempotency_key,
                               tool, args, validation, task_id=None, linked_operation=None):
        """Private host primitive; registered format plugins validate bytes first.

        Never expose this method as a model/API tool or accept a model's validation claim.
        """
        if not isinstance(data,bytes) or len(data)>50_000_000:
            raise HarnessError('file_limit','Staged artifact exceeds the 50 MB limit.')
        if not idempotency_key or len(idempotency_key) > 160:
            raise HarnessError('invalid_patch', 'A bounded idempotency key is required.')
        project_path(self.root, path)
        args_hash = digest(canonical(args).encode())
        with self.transaction():
            existing = self.db.execute('SELECT * FROM operations WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                if existing['args_hash'] != args_hash:
                    raise HarnessError('idempotency_conflict', 'This key was already used for different arguments.')
                return dict(existing)
            old = self.candidate(path)
            from .chats import current_chat,owner
            chat=current_chat(self)
            if task_id:
                row=self.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone()
                if row:chat=row[0]
            if old:
                pending_owner=owner(self,old['operation_id'])
                if pending_owner and pending_owner['id']!=chat:
                    raise HarnessError('working_copy_owned','This working copy belongs to chat: '+pending_owner['title']+'. Return to that chat or apply it there before editing.')
            actual = old['candidate_hash'] if old else self.file_hash(path)
            if actual != expected_revision:
                raise HarnessError('stale_revision', 'The target does not exist: expected_revision must be null, not a source hash.' if actual is None else 'Refresh the target artifact before staging this edit.')
            if tool == 'markdown.stage':
                from .annotations import validate_patch_scope
                validate_patch_scope(self, task_id, path, expected_revision, data.decode('utf-8'))
            elif tool=='word.stage' and task_id:
                from .annotations import validate_word_scope
                from .word.contracts import StageBatch
                request=args['request']
                operations=StageBatch(**request).operations if request['tool']=='word.stage_batch' else []
                validate_word_scope(self,task_id,path,expected_revision,operations)
            base = old['base_hash'] if old else actual
            candidate_hash = self.put_blob(data)
            key = str(uuid.uuid4())
            self.db.execute('''INSERT INTO operations(id,idempotency_key,args_hash,args,task_id,tool,version,status,path,input_hash,candidate_hash,linked_operation,created)
                            VALUES(?,?,?,?,?,?,1,'planned',?,?,?,?,?)''',
                            (key,idempotency_key,args_hash,canonical(args),task_id,tool,path,base,candidate_hash,linked_operation,time.time()))
            self.db.execute('INSERT INTO operation_chats VALUES(?,?)',(key,chat))
            self.event(key,'planned')
            self.event(key,'staged', {'revision':candidate_hash})
            self.event(key,'validated', {**validation,'bytes':len(data)})
            self.db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)', (path,key,base,candidate_hash))
            self.node('file:'+path,'file',{'path':path},base)
            self.node('revision:'+candidate_hash,'revision',{'path':path},candidate_hash)
            self.edge('file:'+path,'revision:'+candidate_hash,'contains',key)
            self.edge('receipt:'+key,'revision:'+candidate_hash,'produced',key)
            if task_id:self.edge('task:'+task_id,'receipt:'+key,'executed',key)
            if task_id:
                self.db.execute('UPDATE task_scopes SET head_revision=? WHERE task_id=?', (candidate_hash, task_id))
        return self.operation(key)

    def apply(self, operation_id: str, *, expected_candidate: str, action: str, fault=None):
        """Only UI calls this method. Model tools never receive this capability."""
        with self.lock:
            op = self.operation(operation_id)
            if op['candidate_hash'] != expected_candidate:
                raise HarnessError('stale_revision', 'The reviewed candidate changed.')
            if op['status'] == 'committed':
                return op
            from .chats import current_chat,owner
            pending_owner=owner(self,operation_id)
            if pending_owner and pending_owner['id']!=current_chat(self):
                raise HarnessError('working_copy_owned','Apply this working copy from its owning chat: '+pending_owner['title'])
            if op['status'] != 'validated':
                raise HarnessError('invalid_state', 'Only a validated candidate can be applied.')
            pending=self.candidate(op['path'])
            if not pending or pending['operation_id'] != operation_id:
                raise HarnessError('stale_revision', 'A newer working copy exists.')
            target = project_path(self.root, op['path'])
            current = self.file_hash(op['path'])
            if current != op['input_hash']:
                raise HarnessError('external_change', 'Original changed outside the harness. Review before applying.')
            if op['tool']=='artifact.remove':
                from .removal import apply_removal
                return apply_removal(self,op,action,fault)
            required = 'save' if current is None else 'apply'
            if action != required:
                raise HarnessError('approval_required', f'Explicit {required.title()} is required.')
            if not target.parent.is_dir():
                raise HarnessError('not_found', 'Destination directory must already exist.')
            candidate = self.blob(op['candidate_hash'])
            backup = self.put_blob(target.read_bytes()) if current is not None else None
            with self.transaction():
                self.db.execute('UPDATE operations SET backup_hash=? WHERE id=?',(backup,operation_id))
                self.event(operation_id,'commit_pending',{'action':action,'candidate':expected_candidate})
            if fault: fault('before_replace')
            # Check again after the journal is durable, before replacing original bytes.
            if self.file_hash(op['path']) != current:
                with self.transaction(): self.event(operation_id,'recovery_required',{'reason':'external_change'})
                raise HarnessError('external_change', 'Original changed during approval.')
            atomic_write(target,candidate)
            if fault: fault('after_replace')
            if self.file_hash(op['path']) != expected_candidate:
                with self.transaction(): self.event(operation_id,'recovery_required')
                raise HarnessError('readback_failed','Committed bytes differ; recovery required.')
            with self.transaction():
                self.event(operation_id,'committed',{'readback_hash':expected_candidate})
                self.db.execute('DELETE FROM candidates WHERE operation_id=?',(operation_id,))
                self.node('file:'+op['path'],'file',{'path':op['path']},expected_candidate)
            return self.operation(operation_id)

    def recover(self):
        with self.transaction():
            for row in self.db.execute("SELECT * FROM operations WHERE status='commit_pending'").fetchall():
                actual = self.file_hash(row['path'])
                completed = actual is None if row['tool']=='artifact.remove' else actual==row['candidate_hash']
                if completed:
                    self.event(row['id'],'committed',{'recovered':True,'readback_hash':actual})
                    self.db.execute('DELETE FROM candidates WHERE operation_id=?',(row['id'],))
                    self.node('file:'+row['path'],'file',{'path':row['path'],'removed':row['tool']=='artifact.remove'},actual)
                elif actual == row['input_hash']:
                    self.event(row['id'],'validated',{'recovered':True,'requires_new_apply':True})
                else:
                    self.event(row['id'],'recovery_required',{'actual_hash':actual})

        from .attachments import register_committed_import
        for row in self.db.execute("SELECT * FROM operations WHERE tool='source.import' AND status='committed' AND args!='{}'").fetchall():
            register_committed_import(self,dict(row))

    def undo(self, operation_id: str, idempotency_key: str):
        """Stage restoration or recoverable creation-removal for explicit review."""
        with self.lock:
            op = self.operation(operation_id)
            if op['args']=='{}':raise HarnessError('undo_unavailable','Recovery content was erased by Forget project memory.')
            if op['status'] != 'committed':
                raise HarnessError('undo_unavailable','Only committed operations can be undone.')
            if op['tool']=='artifact.remove':
                if self.candidate(op['path']) or self.file_hash(op['path']) is not None:
                    raise HarnessError('external_change','Restore would overwrite a file or working copy.')
                return self._stage_restore(op,None,idempotency_key)
            if not op['backup_hash']:
                from .removal import stage_removal
                return stage_removal(self,op,idempotency_key)
            if self.candidate(op['path']) or self.file_hash(op['path']) != op['candidate_hash']:
                raise HarnessError('external_change','Undo would overwrite a later revision or working copy.')
            return self._stage_restore(op,op['candidate_hash'],idempotency_key)

    def _stage_restore(self, op, expected_revision, idempotency_key):
        data=self.blob(op['backup_hash'])
        if op['path'].endswith('.md'):
            return self.stage(path=op['path'],text=data.decode('utf-8'),expected_revision=expected_revision,
                              idempotency_key=idempotency_key,linked_operation=op['id'])
        if not op['path'].lower().endswith(('.docx','.pdf')):
            raise HarnessError('undo_unavailable','No restoration adapter for this format.')
        args={'path':op['path'],'restore_hash':op['backup_hash'],'expected_revision':expected_revision,'linked_operation':op['id']}
        return self._stage_validated_bytes(path=op['path'],data=data,expected_revision=expected_revision,
                    idempotency_key=idempotency_key,linked_operation=op['id'],tool='pdf.restore' if op['path'].lower().endswith('.pdf') else 'word.restore',args=args,
                    validation={'exact_recorded_backup':op['backup_hash']})

    def message(self, role: str, content: str, task_id=None, chat_id=None, *, recovery_state=None):
        with self.transaction():
            cursor = self.db.execute('INSERT INTO messages(task_id,role,content,created) VALUES(?,?,?,?)',
                                    (task_id,role,content,time.time()))
            from .chats import current_chat
            if not chat_id and task_id:
                row=self.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone()
                chat_id=row[0] if row else None
            chat_id=chat_id or current_chat(self)
            self.db.execute('INSERT INTO message_chats VALUES(?,?)',(cursor.lastrowid,chat_id))
            self.db.execute('UPDATE chats SET updated=? WHERE id=?',(time.time(),chat_id))
            if recovery_state is not None:
                if not task_id:raise HarnessError('task_state','Recovery facts require a task.')
                state={**recovery_state,'version':1,'message_id':cursor.lastrowid,
                       'ledger_cursor':self.db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0]}
                self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('task_recovery:'+task_id,canonical(state)))
            return cursor.lastrowid

    def search(self, query: str, limit=10):
        from .chats import current_chat
        chat_id=current_chat(self)
        tokens = query.split()[:12]
        if not tokens: return []
        escaped = ' OR '.join('"'+t.replace('"','""')+'"' for t in tokens)
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT m.id,m.task_id,m.role,substr(m.content,1,2000) AS content,length(m.content)>2000 AS truncated,m.created FROM history_fts f JOIN messages m ON m.id=f.rowid JOIN message_chats c ON c.message_id=m.id
                         WHERE history_fts MATCH ? AND c.chat_id=? ORDER BY rank LIMIT ?''',(escaped,chat_id,min(max(limit,1),30)))]

    def history_window(self, message_id: int, radius=2):
        """Bounded chronological neighbors from the current chat only."""
        from .chats import current_chat
        if type(radius) is not int or not 0<=radius<=5:
            raise HarnessError('history_limit','Choose zero to five surrounding messages per side.')
        chat_id=current_chat(self)
        columns='m.id,m.task_id,m.role,substr(m.content,1,2000) AS content,length(m.content)>2000 AS truncated,m.created'
        with self.lock:
            anchor=self.db.execute(f'SELECT {columns} FROM messages m JOIN message_chats c ON c.message_id=m.id WHERE m.id=? AND c.chat_id=?',(message_id,chat_id)).fetchone()
            if not anchor:raise HarnessError('not_found','Message not found in this chat.')
            before=self.db.execute(f'SELECT {columns} FROM messages m JOIN message_chats c ON c.message_id=m.id WHERE m.id<? AND c.chat_id=? ORDER BY m.id DESC LIMIT ?',(message_id,chat_id,radius)).fetchall()
            after=self.db.execute(f'SELECT {columns} FROM messages m JOIN message_chats c ON c.message_id=m.id WHERE m.id>? AND c.chat_id=? ORDER BY m.id LIMIT ?',(message_id,chat_id,radius)).fetchall()
            return {'anchor':message_id,'radius':radius,'characters_per_message':2000,
                    'messages':[dict(row) for row in [*reversed(before),anchor,*after]]}

    def decision(self, content: str, source_message: int, decision_id=None, scope="chat", expected_version=None):
        from .chats import current_chat
        chat_id=current_chat(self)
        if scope not in ('chat','project'):raise HarnessError('memory_scope','Choose chat or project memory scope.')
        content=content.strip()
        if not content or len(content)>4000:raise HarnessError('memory_limit','A confirmed decision must contain 1–4000 characters.')
        with self.transaction():
            source = self.db.execute("SELECT * FROM messages WHERE id=? AND role='user'",(source_message,)).fetchone()
            source_chat=self.db.execute('SELECT chat_id FROM message_chats WHERE message_id=?',(source_message,)).fetchone()
            if not source_chat or source_chat[0]!=chat_id:raise HarnessError('chat_scope','Confirm decisions from a user message in this chat.')
            if not source: raise HarnessError('provenance_required','A user message is required to confirm a decision.')
            key = decision_id or str(uuid.uuid4())
            prior_scope=self.db.execute('SELECT * FROM decision_scopes WHERE decision_id=?',(key,)).fetchone()
            if prior_scope and prior_scope['scope']=='chat' and prior_scope['chat_id']!=chat_id:
                raise HarnessError('chat_scope','A chat cannot overwrite another chat’s private decision.')
            old = self.db.execute('SELECT * FROM decisions WHERE id=?',(key,)).fetchone()
            if expected_version is not None and (not old or old['version']!=expected_version):
                raise HarnessError('decision_stale','This decision changed. Reload its current version before correcting it.')
            if decision_id and not old:raise HarnessError('not_found','Decision not found.')
            if old and prior_scope:
                self.db.execute('INSERT OR IGNORE INTO decision_versions VALUES(?,?,?,?,?,?,?)',(key,old['version'],old['content'],old['source_message'],prior_scope['scope'],prior_scope['chat_id'],time.time()))
            version = old['version']+1 if old else 1
            self.db.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,1)',(key,content,source_message,version))
            self.db.execute('INSERT OR REPLACE INTO decision_scopes VALUES(?,?,?)',(key,scope,chat_id if scope=='chat' else None))
            self.db.execute('INSERT INTO decision_versions VALUES(?,?,?,?,?,?,?)',(key,version,content,source_message,scope,chat_id if scope=='chat' else None,time.time()))
            self.node('decision:'+key,'decision',{'content':content,'source_message':source_message,'version':version})
            return key

    def checkpoint(self, task_id=None, *, summary=None, summary_error=None):
        from .chats import current_chat,decisions
        chat_id=current_chat(self)
        if task_id:
            row=self.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone()
            if row:chat_id=row[0]
        with self.transaction():
            state = {
                'project_id':self.id,'task_id':task_id,'chat_id':chat_id,
                'decisions':decisions(self,chat_id),
                'candidates':[dict(r) for r in self.db.execute('SELECT * FROM candidates')],
                'scopes':[dict(r) for r in self.db.execute('SELECT s.* FROM task_scopes s JOIN task_chats c ON c.task_id=s.task_id WHERE c.chat_id=?',(chat_id,))],
                'tasks':[dict(r) for r in self.db.execute("SELECT t.* FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE c.chat_id=? AND t.status NOT IN ('completed','cancelled')",(chat_id,))],
                'recovery':[{'task_id':r['id'],**json.loads(r['value'])} for r in self.db.execute(
                    "SELECT t.id,m.value FROM tasks t JOIN task_chats c ON c.task_id=t.id JOIN meta m ON m.key='task_recovery:'||t.id WHERE c.chat_id=? AND t.status NOT IN ('completed','cancelled')",(chat_id,)) if json.loads(r['value']).get('unresolved')],
                'ledger_cursor':self.db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0],
                'history_cursor':self.db.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0],
                'summary':summary,'summary_error':summary_error,'fallback': summary is None,
            }
            cur=self.db.execute('INSERT INTO checkpoints(task_id,body,created) VALUES(?,?,?)',(task_id,canonical(state),time.time()))
            return {'generation':cur.lastrowid,**state}

    def snapshot(self):
        from .chats import current_chat,list_chats,decisions,owner,active_task
        chat=current_chat(self)
        with self.lock:
            result={'project_id':self.id,'root':str(self.root),'chat_id':chat,'chats':list_chats(self),'active_task_id':active_task(self,chat)}
            for table,mapping,column in [('messages','message_chats','message_id'),('tasks','task_chats','task_id'),('annotations','annotation_chats','annotation_id')]:
                result[table]=[dict(r) for r in self.db.execute(f'SELECT t.* FROM {table} t JOIN {mapping} c ON c.{column}=t.id WHERE c.chat_id=? ORDER BY t.rowid DESC LIMIT 200',(chat,))]
            for table in ['candidates','operations','nodes','edges','task_annotations','task_scopes']:
                result[table]=[dict(r) for r in self.db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 200')]
            result['checkpoints']=[dict(r) for r in self.db.execute("SELECT * FROM checkpoints WHERE json_extract(body,'$.chat_id')=? ORDER BY generation DESC LIMIT 30",(chat,))]
            result['decisions']=decisions(self,chat)
            result['attachments']=[dict(r) for r in self.db.execute('SELECT * FROM attachments WHERE chat_id=? ORDER BY created',(chat,))]
            for candidate in result['candidates']:
                candidate['owner_chat']=owner(self,candidate['operation_id'])
                candidate['removal']=self.operation(candidate['operation_id'])['tool']=='artifact.remove'
            return result
