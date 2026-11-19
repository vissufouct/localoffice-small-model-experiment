"""Owned, revision-bound preview facts; never visual review or completion proof."""
import io
import json
import math
import re
from ..chats import current_chat
from ..errors import HarnessError
from ..paths import project_path
from ..storage import canonical,digest

PREFIX='word_render_context:'
MAX_RECORD_BYTES=8192
MAX_SUMMARY_BYTES=300
MAX_PDF_BYTES=50_000_000
_HASH=re.compile(r'[0-9a-f]{64}\Z')


def _require(condition,message,code='state_corrupt'):
    if not condition:raise HarnessError(code,message)


def _hash(value):
    return isinstance(value,str) and _HASH.fullmatch(value) is not None


def _string(value,limit):
    return isinstance(value,str) and len(value)<=limit and not any(0xD800<=ord(c)<=0xDFFF for c in value)


def _json(value,depth=0):
    _require(depth<=16,'Render provenance nesting exceeds its bound.')
    if value is None or type(value) in (bool,int):return
    if type(value) is float:
        _require(math.isfinite(value),'Nonfinite render provenance.');return
    if isinstance(value,str):
        _require(_string(value,MAX_RECORD_BYTES),'Invalid render provenance string.');return
    if isinstance(value,list):
        for child in value:_json(child,depth+1)
        return
    _require(isinstance(value,dict),'Render provenance must be JSON.')
    for key,child in value.items():
        _require(_string(key,256),'Invalid render provenance key.');_json(child,depth+1)


def _owner(store,task_id):
    row=store.db.execute('SELECT t.artifact,c.chat_id FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE t.id=?',(task_id,)).fetchone()
    _require(row is not None and row['chat_id']==current_chat(store),
             'Render context belongs to its original task and chat.','render_context_scope')
    return dict(row)


def _result(store,result):
    required={'path','revision','pdf_hash','pages','configuration','warnings','cached'}
    _require(isinstance(result,dict) and required<=result.keys() and
             not result.keys()-required-{'field_snapshot_hash','field_update'},'Invalid render receipt fields.')
    _json(result)
    _require(_string(result['path'],1024) and result['path'].lower().endswith('.docx'), 'Invalid render target.')
    project_path(store.root,result['path'])
    _require(_hash(result['revision']) and _hash(result['pdf_hash']),'Invalid render hashes.')
    _require(type(result['pages']) is int and 1<=result['pages']<=2000,'Invalid render page count.')
    _require(type(result['cached']) is bool,'Invalid render cache flag.')
    config=result['configuration']
    _require(isinstance(config,dict) and _string(config.get('renderer'),512) and bool(config['renderer']),
             'Missing renderer identity.')
    _require(isinstance(result['warnings'],list) and len(result['warnings'])<=32 and
             all(_string(warning,1000) for warning in result['warnings']),'Invalid render warnings.')
    _require(('field_snapshot_hash' in result)==('field_update' in result),'Incomplete field snapshot provenance.')
    if 'field_snapshot_hash' in result:
        _require(_hash(result['field_snapshot_hash']) and isinstance(result['field_update'],dict),'Invalid field snapshot provenance.')


def _blob(store,key):
    try:data=store.blob(key)
    except (OSError,ValueError) as exc:
        raise HarnessError('state_corrupt','Render provenance blob is unavailable.') from exc
    _require(0<len(data)<=MAX_PDF_BYTES,'Render provenance blob exceeds its size bound.')
    return data


def _current(store,path):
    project_path(store.root,path)
    candidate=store.candidate(path)
    return candidate['candidate_hash'] if candidate else store.file_hash(path)


def _load(store,task_id,owner):
    row=store.db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+task_id,)).fetchone()
    if row is None:return None
    try:
        raw=row[0]
        _require(isinstance(raw,str) and len(raw.encode())<=MAX_RECORD_BYTES,'Render context exceeds its byte bound.')
        envelope=json.loads(raw)
        _require(isinstance(envelope,dict) and set(envelope)=={'state','hash'},'Invalid render envelope.')
        state=envelope['state']
        _require(_hash(envelope['hash']) and envelope['hash']==digest(canonical(state).encode()),'Render context integrity hash mismatch.')
        _require(isinstance(state,dict) and set(state)=={'version','task_id','chat_id','result'},'Invalid render state.')
        _require(type(state['version']) is int and state['version']==1 and
                 state['task_id']==task_id and state['chat_id']==owner['chat_id'],'Render context ownership mismatch.')
        _result(store,state['result'])
        return state
    except (ValueError,TypeError,KeyError,UnicodeError,RecursionError) as exc:
        raise HarnessError('state_corrupt','Malformed render context.') from exc


def remember(store,task_id,result):
    """Record only a successful current-target receipt; return input unchanged.

    The caller is the owner-checked render boundary, not model-generated text.
    Full bounded provenance is retained, including optional private field-update
    evidence. Preview rendering itself does not update the target DOCX caches.
    """
    if task_id is None:return result
    with store.transaction():
        owner=_owner(store,task_id)
        _load(store,task_id,owner)  # Never overwrite corrupt durable evidence.
        _result(store,result)
        _require(owner['artifact']==result['path'],'Render receipt does not address the active task target.','render_context_scope')
        _require(result['revision']==_current(store,result['path']),'Render receipt revision is no longer current.','stale_revision')
        state={'version':1,'task_id':task_id,'chat_id':owner['chat_id'],'result':result}
        encoded=canonical({'state':state,'hash':digest(canonical(state).encode())})
        _require(len(encoded.encode())<=MAX_RECORD_BYTES,'Render context exceeds its byte bound.','render_context_limit')
        pdf=_blob(store,result['pdf_hash'])
        try:
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(pdf))
            _require(not reader.is_encrypted,'Encrypted render PDF is unsupported.')
            _require(len(reader.pages)==result['pages'],'Render receipt page count differs from its PDF.')
        except HarnessError:raise
        except Exception as exc:
            raise HarnessError('state_corrupt','Invalid rendered PDF provenance.') from exc
        if 'field_snapshot_hash' in result:_blob(store,result['field_snapshot_hash'])
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(PREFIX+task_id,encoded))
    return result


def summary(store,task_id,path,revision):
    """Return <=300 UTF-8 bytes of current render facts, or omit stale facts."""
    if task_id is None:return None
    with store.transaction():
        owner=_owner(store,task_id);state=_load(store,task_id,owner)
        if state is None:return None
        receipt=state['result']
        if (revision is None or path!=owner['artifact'] or path!=receipt['path'] or revision!=receipt['revision']):return None
        if revision!=_current(store,path):return None
        _blob(store,receipt['pdf_hash'])
        if 'field_snapshot_hash' in receipt:_blob(store,receipt['field_snapshot_hash'])
        renderer=receipt['configuration']['renderer']
        abbreviated=renderer
        while len(abbreviated.encode())>48:abbreviated=abbreviated[:-1]
        result={'revision':revision,'pages':receipt['pages'],'renderer':abbreviated,
                'preview_does_not_update_docx_caches':True,'visual_approval':False}
        if abbreviated!=renderer:result['renderer_abbreviated']=True
        _require(len(canonical(result).encode())<=MAX_SUMMARY_BYTES,'Render summary exceeds its bound.')
        return result
