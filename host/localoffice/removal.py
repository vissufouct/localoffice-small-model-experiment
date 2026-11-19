"""Host-only undo of file creation; bytes retained in the blob recovery store."""
import os
import time
import uuid
from .storage import canonical,digest
from .paths import project_path
from .errors import HarnessError
from .chats import current_chat


def stage_removal(store,original,idempotency_key):
    previous=store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
    if previous:
        if previous['linked_operation']!=original['id']:raise HarnessError('idempotency_conflict','Undo identity belongs to another operation.')
        return dict(previous)
    path=original['path']
    if not ((original['tool']=='markdown.stage' and path.endswith('.md')) or (original['tool'] in ('word.stage','word.restore') and path.lower().endswith('.docx')) or (original['tool'] in ('pdf.export','pdf.restore') and path.lower().endswith('.pdf'))):raise HarnessError('undo_unavailable','Creation undo requires a supported generated artifact.')
    if store.candidate(path) or store.file_hash(path)!=original['candidate_hash']:
        raise HarnessError('external_change','Undo would remove a later revision or working copy.')
    key=str(uuid.uuid4());args={'path':path,'undo_creation':original['id']};revision=original['candidate_hash']
    store.blob(revision)  # Validate recoverable content before staging removal.
    with store.transaction():
        store.db.execute('''INSERT INTO operations(id,idempotency_key,args_hash,args,tool,version,status,path,input_hash,candidate_hash,backup_hash,linked_operation,created)
                            VALUES(?,?,?,?,'artifact.remove',1,'planned',?,?,?,?,?,?)''',
                         (key,idempotency_key,digest(canonical(args).encode()),canonical(args),path,revision,revision,revision,original['id'],time.time()))
        store.db.execute('INSERT INTO operation_chats VALUES(?,?)',(key,current_chat(store)))
        store.event(key,'planned',{'host_only':True,'recoverable_removal':True})
        store.event(key,'staged');store.event(key,'validated')
        store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',(path,key,revision,revision))
    return store.operation(key)


def apply_removal(store,op,action,fault=None):
    if action!='remove':raise HarnessError('approval_required','Explicit Remove file is required to undo creation.')
    target=project_path(store.root,op['path'],must_exist=True)
    store.blob(op['backup_hash'])
    with store.transaction():store.event(op['id'],'commit_pending',{'action':'remove','recovery_blob':op['backup_hash']})
    if fault:fault('before_replace')
    if store.file_hash(op['path'])!=op['input_hash']:
        with store.transaction():store.event(op['id'],'recovery_required',{'reason':'external_change'})
        raise HarnessError('external_change','File changed during removal approval.')
    target.unlink()
    descriptor=os.open(target.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    if fault:fault('after_replace')
    if target.exists():raise HarnessError('external_change','File reappeared during removal; recovery required.')
    with store.transaction():
        store.event(op['id'],'committed',{'file_absent':True,'recovery_blob':op['backup_hash']})
        store.db.execute('DELETE FROM candidates WHERE operation_id=?',(op['id'],))
        store.node('file:'+op['path'],'file',{'path':op['path'],'removed':True},None)
    return store.operation(op['id'])
