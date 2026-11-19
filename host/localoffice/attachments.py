"""Explicit user uploads become immutable, chat-owned source registrations."""
import json
import re
import time
import unicodedata
import uuid
from pathlib import Path
from .chats import current_chat
from .errors import HarnessError
from .paths import project_path
from .storage import canonical,digest

ALLOWED={'.pdf','.md','.txt','.csv','.docx','.pptx','.xlsx','.png','.jpg','.jpeg','.webp'}

def import_attachment(store,filename,data):
    if not data or len(data)>50_000_000:raise HarnessError('attachment_limit','Attach a nonempty file up to 50 MB.')
    name=unicodedata.normalize('NFKC',Path(filename.replace('\\','/')).name)
    name=re.sub(r'[^\w. ()-]','_',name).lstrip('.')[:160]
    suffix=Path(name).suffix.lower()
    if not name or suffix not in ALLOWED:raise HarnessError('attachment_format','Supported inputs: PDF, Markdown, text, CSV, Office files and common images. Scripts and executables are not accepted.')
    chat=current_chat(store);content_hash=digest(data)
    epoch_row=store.db.execute("SELECT value FROM meta WHERE key='memory_epoch'").fetchone()
    epoch=epoch_row[0] if epoch_row else ''
    key='attachment:'+chat+':'+(epoch+':' if epoch else '')+content_hash
    old=store.db.execute('SELECT * FROM attachments WHERE chat_id=? AND content_hash=?',(chat,content_hash)).fetchone()
    if old:
        if store.file_hash(old['path'])!=content_hash:raise HarnessError('attachment_changed','The registered input file changed. Restore it or import it with an explicit new source revision.')
        return dict(old)
    prefix='input/'+chat+('/'+epoch if epoch else '')
    directory=project_path(store.root,prefix)
    directory.mkdir(parents=True,exist_ok=True)
    target=f'{prefix}/{Path(name).stem}-{content_hash[:12]}{suffix}'
    args={'chat_id':chat,'original_name':name,'size':len(data),'path':target,'content_hash':content_hash}
    with store.lock:
        existing=store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(key,)).fetchone()
        if existing:
            operation_id=existing['id']
            target=existing['path']
            args=json.loads(existing['args'])
            if existing['status'] in ('planned','staged'):
                if store.file_hash(target) is not None:
                    raise HarnessError('input_conflict','An interrupted import conflicts with a file now present at its destination.')
                store.put_blob(data)
                with store.transaction():
                    store.event(operation_id,'staged',{'recovered_upload':True})
                    store.event(operation_id,'validated',{'bytes':len(data),'no_execution':True})
                    store.db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,NULL,?)',(target,operation_id,content_hash))
        else:
            if store.file_hash(target) is not None:raise HarnessError('input_conflict','Input destination already exists; it was not overwritten.')
            operation_id=str(uuid.uuid4())
            with store.transaction():
                store.db.execute('''INSERT INTO operations(id,idempotency_key,args_hash,args,tool,version,status,path,input_hash,candidate_hash,created)
                    VALUES(?,?,?,?,'source.import',1,'planned',?,NULL,?,?)''',
                    (operation_id,key,digest(canonical(args).encode()),canonical(args),target,content_hash,time.time()))
                store.db.execute('INSERT INTO operation_chats VALUES(?,?)',(operation_id,chat))
                store.event(operation_id,'planned',{'explicit_user_upload':True})
            store.put_blob(data)
            with store.transaction():
                store.event(operation_id,'staged',{'candidate_hash':content_hash})
                store.event(operation_id,'validated',{'bytes':len(data),'no_execution':True})
                store.db.execute('INSERT INTO candidates VALUES(?,?,NULL,?)',(target,operation_id,content_hash))
        store.apply(operation_id,expected_candidate=content_hash,action='save')
        register_committed_import(store,store.operation(operation_id))
        return dict(store.db.execute('SELECT * FROM attachments WHERE id=?',(operation_id,)).fetchone())


def register_committed_import(store,operation):
    args=json.loads(operation['args'])
    with store.transaction():
        store.db.execute('INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?,?,?)',
                         (operation['id'],args['chat_id'],operation['path'],args['original_name'],operation['candidate_hash'],args['size'],operation['created']))
        store.node('attachment:'+operation['id'],'source',{'path':operation['path'],'chat_id':args['chat_id'],'original_name':args['original_name']},operation['candidate_hash'])
        store.edge('chat:'+args['chat_id'],'attachment:'+operation['id'],'uses-source',operation['id'])
