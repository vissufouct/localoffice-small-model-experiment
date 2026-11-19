"""Host-only staging of a PDF from an exact reviewed Word render."""
import io
import json
from pypdf import PdfReader
from ..errors import HarnessError
from ..paths import project_path
from ..storage import canonical,digest
from .contracts import Target


def stage_pdf_export(plugin, *, path, revision, pdf_hash, destination, idempotency_key):
    request=Target(path=path,revision=revision)
    store=plugin.store
    if len(destination)>1024 or '\\' in destination or '\x00' in destination:
        raise HarnessError('path_denied','Use a project-relative PDF filename.')
    output=project_path(store.root,destination)
    destination=output.relative_to(store.root).as_posix()
    if output.suffix.lower()!='.pdf':raise HarnessError('format_unsupported','Choose a .pdf export filename.')
    if not output.parent.is_dir():raise HarnessError('not_found','Choose an existing destination folder inside this project.')
    args={'source_path':request.path,'source_revision':request.revision,'pdf_hash':pdf_hash,'destination':destination}
    with store.lock:
        previous=store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
        if previous:
            if previous['tool']!='pdf.export' or previous['args_hash']!=digest(canonical(args).encode()):
                raise HarnessError('idempotency_conflict','Export identity has different arguments.')
            return dict(previous)
        plugin._bytes(request.path,request.revision)
        if output.exists() or store.candidate(destination):
            raise HarnessError('target_exists','Export filename already exists or has a working copy. Choose a new filename.')
        renders=[json.loads(row[0]) for row in store.db.execute('SELECT body FROM render_cache WHERE revision=?',(request.revision,))]
        rendered=next((r for r in renders if r['pdf_hash']==pdf_hash),None)
        if not rendered:raise HarnessError('stale_render','Export the PDF shown for this exact Word revision.')
        data=store.blob(pdf_hash)
        if not data.startswith(b'%PDF-') or len(PdfReader(io.BytesIO(data)).pages)!=rendered['pages']:
            raise HarnessError('render_failed','Rendered PDF failed export read-back validation.')
        plugin._bytes(request.path,request.revision)
        with store.transaction():
            operation=store._stage_validated_bytes(path=destination,data=data,expected_revision=None,
                idempotency_key=idempotency_key,tool='pdf.export',args=args,
                validation={'pdf':True,'pages':rendered['pages'],'source_revision':request.revision,'render_configuration':rendered['configuration']})
            store.edge('revision:'+operation['candidate_hash'],'revision:'+request.revision,'exported-from',operation['id'])
        return operation
