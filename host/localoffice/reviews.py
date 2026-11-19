"""Revision-bound user attestations, separate from automated visual inspection."""
import json
import time
import uuid
from pathlib import Path
from .chats import current_chat
from .errors import HarnessError
from .paths import project_path
from .storage import canonical,digest


def record_review(store,*,path,revision,phase,outcome,note,view):
    # This API records a user's explicit attestation. It cannot claim model vision.
    source=project_path(store.root,path)
    suffix=source.suffix.lower()
    if suffix not in ('.md','.pdf','.docx'):
        raise HarnessError('review_format','Visual review supports Markdown, PDF and rendered Word previews.')
    if phase not in ('before','during','final') or outcome not in ('accepted','needs_changes'):
        raise HarnessError('review_arguments','Choose a review stage and outcome.')
    if not isinstance(note,str) or len(note)>4000:
        raise HarnessError('review_arguments','Review notes must be at most 4,000 characters.')
    if view.get('renderer')!=('PDF.js' if suffix in ('.pdf','.docx') else 'marked+DOMPurify'):
        raise HarnessError('review_renderer','Review renderer does not match this artifact.')
    if suffix in ('.pdf','.docx') and (type(view.get('page')) is not int or view['page']<1):
        raise HarnessError('review_page','Choose the rendered PDF page.')
    if suffix=='.docx':
        renders=[json.loads(row[0]) for row in store.db.execute('SELECT body FROM render_cache WHERE revision=?',(revision,))]
        rendered=next((r for r in renders if r['pdf_hash']==view.get('pdf_hash')),None)
        if not rendered or view['page']>rendered['pages']:
            raise HarnessError('review_render','Choose a page from a verified render of this Word revision.')
        store.blob(rendered['pdf_hash'])
    with store.lock:
        candidate=store.candidate(path)
        if candidate:
            data=store.blob(candidate['candidate_hash'])
        else:
            source=project_path(store.root,path,must_exist=True)
            if source.stat().st_size>50_000_000:
                raise HarnessError('file_limit','Review source exceeds 50 MB.')
            data=source.read_bytes()
        if digest(data)!=revision:
            raise HarnessError('stale_revision','The preview revision changed. Reopen and review the current version.')
        store.put_blob(data)
        record={'id':str(uuid.uuid4()),'chat_id':current_chat(store),'path':path,'revision':revision,
                'phase':phase,'outcome':outcome,'note':note,'view':view,'reviewer':'user',
                'evidence_kind':'explicit_user_attestation','automated_vision':False,'created':time.time()}
        with store.transaction():
            store.db.execute('INSERT INTO visual_reviews VALUES(?,?,?,?,?)',
                             (record['id'],record['chat_id'],path,revision,canonical(record)))
            store.node('review:'+record['id'],'visual_review',record,revision)
            store.node('revision:'+revision,'revision',{'path':path},revision)
            store.edge('review:'+record['id'],'revision:'+revision,'reviews','user.attestation')
        return record


def list_reviews(store,path):
    project_path(store.root,path)
    candidate=store.candidate(path)
    current=candidate['candidate_hash'] if candidate else store.file_hash(path)
    return [{**json.loads(row['body']),'current_revision':row['revision']==current}
            for row in store.db.execute('SELECT * FROM visual_reviews WHERE path=? ORDER BY rowid DESC LIMIT 100',(path,))]
