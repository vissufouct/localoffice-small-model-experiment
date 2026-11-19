"""Disk-backed complete logical exports; no UI snapshot row limits."""
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path


def export_memory(store):
    # Snapshot all authoritative tables consistently, without loading history into RAM.
    # FTS shadow tables are derived and intentionally excluded.
    descriptor,name=tempfile.mkstemp(prefix='localoffice-memory-',suffix='.json')
    os.close(descriptor)
    snapshot_dir=tempfile.TemporaryDirectory(prefix='localoffice-export-snapshot-')
    snapshot=sqlite3.connect(str(Path(snapshot_dir.name)/'snapshot.sqlite3'))
    try:
        with store.lock:store.db.backup(snapshot)
        tables=[r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'history_fts%' ORDER BY name")]
        snapshot.row_factory=sqlite3.Row
        with open(name,'w') as output:
            output.write(json.dumps({'format':'localoffice-memory','version':1,'project_id':store.id,'scope':'all project chats','blob_policy':'Hashes only; keep .localoffice/blobs for file recovery.'})[:-1]+',"tables":{')
            for index,table in enumerate(tables):
                if index:output.write(',')
                output.write(json.dumps(table)+':[')
                for row_index,row in enumerate(snapshot.execute('SELECT * FROM "'+table.replace('"','""')+'"')):
                    if row_index:output.write(',')
                    output.write(json.dumps(dict(row),ensure_ascii=False))
                output.write(']')
            output.write('}}')
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    finally:
        snapshot.close()
        snapshot_dir.cleanup()


def start_fresh(store):
    from .chats import current_chat,set_active_task
    chat=current_chat(store)
    with store.transaction():
        cursor=store.db.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('history_floor:'+chat,str(cursor)))
        set_active_task(store,None,chat)
        store.checkpoint(summary_error='user started fresh; previous messages remain searchable')
    return {'history_floor':cursor,'durable_state_retained':True}


def forget_project_memory(store,confirmation):
    """Explicit project-wide erasure. Original project/input files are not memory."""
    from .errors import HarnessError
    from .storage import canonical
    from .chats import current_chat
    if confirmation!='FORGET PROJECT MEMORY':
        raise HarnessError('confirmation_required','Type FORGET PROJECT MEMORY to confirm permanent memory deletion.')
    with store.lock:
        if store.db.execute('SELECT 1 FROM candidates LIMIT 1').fetchone() or store.db.execute("SELECT 1 FROM operations WHERE status IN ('commit_pending','recovery_required') LIMIT 1").fetchone():
            raise HarnessError('deletion_deferred','Apply or resolve pending working copies/recovery first. Their evidence is retained until resolved.')
        store.db.execute('PRAGMA secure_delete=ON')
        with store.transaction():
            # Preserve only the operation identities/hashes needed to avoid replaying
            # completed writes. Content-bearing arguments/errors/backups are removed.
            store.db.execute("UPDATE operations SET args='{}',error=NULL,backup_hash=NULL,task_id=NULL,status=CASE WHEN status='committed' THEN status ELSE 'cancelled' END")
            store.db.execute("UPDATE events SET detail='{}'")
            for table in ('word_content_refs','native_calls','word_evidence','image_asset_sources','image_assets','render_cache','task_measurements','messages','message_chats','decisions','decision_scopes','decision_versions','annotations','annotation_chats','task_annotations','task_scopes','task_skills','task_policy','tasks','task_chats','checkpoints','nodes','edges','visual_reviews','attachments'):
                store.db.execute('DELETE FROM '+table)
            store.db.execute("INSERT INTO history_fts(history_fts) VALUES('rebuild')")
            store.db.execute("UPDATE chats SET title='New chat'")
            store.db.execute("DELETE FROM meta WHERE key LIKE 'active_task:%' OR key LIKE 'history_floor:%' OR key='active_artifact' OR key LIKE 'task_recovery:%' OR key LIKE 'task_protocol:%' OR key LIKE 'task_sampling:%' OR key LIKE 'task_content_mode:%'")
            # Task projections can contain source quotations, draft proposals
            # and read excerpts even after their underlying tables are erased.
            for prefix in ('word_render_context:','word_read_context:','word_active_literal_plan:',
                           'word_content_coverage:','word_creation_repair:','word_tools:',
                           'word_content_plan_repair:','word_pending_content_plan:','word_draft_decisions:',
                           'word_content_plan_failure:','word_source_quote_selection:',
                           'word_creation_block_result:','word_creation_style_result:','word_creation_submission:',
                           'word_requirements:','word_requirements_history:','word_requirements_result:',
                           'draft_fragment:','draft_fragment_history:','draft_fragment_result:',
                           'task_draft_budget:'):
                store.db.execute('DELETE FROM meta WHERE key LIKE ?',(prefix+'%',))
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('forget_pending','1')")
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('memory_forgotten','1')")
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('memory_epoch',?)",(uuid.uuid4().hex,))
        finish_forget(store)
    return {'status':'forgotten','files_unchanged':True,'undo_backups_removed':True,'retained':'Project/chat IDs, settings, non-content ledger IDs, paths, hashes and timestamps. User-created exports and external backups are not erased.'}


def finish_forget(store):
    """Idempotently finish an interrupted erasure before serving this project."""
    flag=store.db.execute("SELECT value FROM meta WHERE key='forget_pending'").fetchone()
    if not flag or flag[0]!='1':return
    # No pending candidates exist when the intent is committed. Only managed
    # content-addressed blobs are removed; project files and skill sources remain.
    for path in store.blobs.iterdir():
        if len(path.name)==64 and all(c in '0123456789abcdef' for c in path.name):path.unlink()
    descriptor=os.open(store.blobs,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    store.db.execute('VACUUM')
    with store.transaction():store.db.execute("DELETE FROM meta WHERE key='forget_pending'")
    store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
