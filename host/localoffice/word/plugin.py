"""Host-mediated Word tools for validated working copies and bounded reads."""
import json
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile
import io
import posixpath
from xml.etree import ElementTree as ET
from ..errors import HarnessError
from ..paths import project_path
from ..storage import canonical, digest
from .contracts import ListObjects, FindText, ReadObjects, FormatTextCall, ReplaceTextCall, StageBatch, CreateDocument, RefreshFields

ROOT = Path(__file__).resolve().parents[3]
_UNGATED_PAGE_REQUIREMENTS = object()


class PageRequirementRejected(HarnessError):
    """Host stamp rejected before preparation retention; not a failed mutation."""


class WordPlugin:
    def __init__(self, store):
        self.store = store;self.busy=False
        self.dotnet = ROOT/'.runtime/dotnet/dotnet'
        self.worker = ROOT/'workers/openxml/bin/Debug/net10.0/LocalOffice.OpenXml.dll'

    def _bytes(self, path, revision):
        target = project_path(self.store.root, path)
        pending = self.store.candidate(path)
        if pending:
            data = self.store.blob(pending['candidate_hash'])
        else:
            if not target.is_file(): raise HarnessError('not_found','Word source does not exist.')
            if target.stat().st_size > 50_000_000: raise HarnessError('file_limit','Word source exceeds 50 MB.')
            data = target.read_bytes()
        if digest(data) != revision: raise HarnessError('stale_revision','Refresh the Word source revision.')
        return data

    def _execute(self, request, action, field_snapshot=None):
        if not self.dotnet.is_file() or not self.worker.is_file():
            raise HarnessError('worker_unavailable','Build the pinned Open XML worker first.')
        if action=='create':
            project_path(self.store.root,request.path)
            if self.store.candidate(request.path) or self.store.file_hash(request.path) is not None:
                raise HarnessError('already_exists','Creation cannot replace an existing artifact or working copy.')
            data=b''
        else:data = self._bytes(request.path, request.revision)
        with tempfile.TemporaryDirectory(prefix='word-',dir=self.store.state) as folder:
            folder = Path(folder); source = folder/'input.docx'; output = folder/'output.docx'
            source.write_bytes(data)
            message = dict(input=str(source),output=str(output),revision=request.revision,
                           action=action,arguments=request.model_dump(exclude_none=True))
            message['arguments'].pop('literal_refs',None)
            if field_snapshot is not None:
                snapshot=folder/'fields.docx';snapshot.write_bytes(field_snapshot)
                message['field_snapshot']=str(snapshot)
            image_media={}
            if action=='patch':
                from ..assets import AssetPlugin
                for op in request.operations:
                    if op.op not in ('insert_image','replace_image'):continue
                    enabled=self.store.db.execute("SELECT value FROM meta WHERE key='plugin:assets'").fetchone()
                    if enabled and enabled[0]!='enabled':
                        raise HarnessError('plugin_disabled','assets is disabled.')
                    metadata,image=AssetPlugin(self.store).placement_bytes(op.asset_id)
                    image_path=folder/'placement-image';image_path.write_bytes(image)
                    media='word/media/localoffice-'+digest(image)+('.png' if metadata['mime_type']=='image/png' else '.jpg')
                    image_media[media]=image
                    message['image']={'input':str(image_path),'hash':digest(image),'media':media,
                                      'mime':metadata['mime_type'],'width':metadata['width_px'],'height':metadata['height_px']}
            env={**os.environ,'DOTNET_CLI_TELEMETRY_OPTOUT':'1','DOTNET_GENERATE_ASPNET_CERTIFICATE':'false'}
            try:
                process = subprocess.run([str(self.dotnet),str(self.worker)],input=canonical(message),
                                         capture_output=True,text=True,timeout=30,env=env)
            except subprocess.TimeoutExpired as exc:
                raise HarnessError('worker_timeout','Word operation exceeded 30 seconds.') from exc
            try: response=json.loads(process.stdout)
            except (ValueError,TypeError) as exc:
                raise HarnessError('worker_protocol','Word worker returned an invalid response.') from exc
            if process.returncode or not response.get('ok'):
                raise HarnessError('word_operation',response.get('error','Word worker failed.'))
            result=response['result']
            if len(canonical(result).encode())>16_384:
                raise HarnessError('result_limit','Narrow the Word read/list request to fit the result budget.')
            candidate=output.read_bytes() if action in ('patch','create','refresh_fields') else None
            if candidate is not None:
                if digest(candidate)!=result['candidate_revision']:
                    raise HarnessError('readback_failed','Word worker output hash mismatch.')
                if action=='create':return result,candidate
                if result.get('unchanged'):
                    if candidate!=data:raise HarnessError('preservation_failed','Unchanged receipt modified the document.')
                    return result,candidate
                # Independently verify the worker's package preservation claim.
                with zipfile.ZipFile(io.BytesIO(data)) as before,zipfile.ZipFile(io.BytesIO(candidate)) as after:
                    added=set(after.namelist())-set(before.namelist())
                    allowed_added=set(image_media)
                    if image_media:allowed_added.add(result['image_relationships_part'])
                    if set(before.namelist())-set(after.namelist()) or not added<=allowed_added:
                        raise HarnessError('preservation_failed','Patch changed an unexpected package entry.')
                    for name,image in image_media.items():
                        if after.read(name)!=image:raise HarnessError('preservation_failed','Embedded image bytes changed.')
                    changed=set(result['changed_parts'])
                    if action=='refresh_fields':
                        allowed={'word/document.xml','word/styles.xml'}
                        ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                        relns='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
                        document=ET.fromstring(before.read('word/document.xml'))
                        ids={n.get(relns+'id') for n in document.iter() if n.tag in (ns+'headerReference',ns+'footerReference')}
                        if 'word/_rels/document.xml.rels' in before.namelist():
                            for rel in ET.fromstring(before.read('word/_rels/document.xml.rels')):
                                if rel.get('Id') in ids and rel.get('TargetMode')!='External' and rel.get('Type','').endswith(('/header','/footer')):
                                    target=rel.get('Target','')
                                    allowed.add(target.lstrip('/') if target.startswith('/') else posixpath.normpath('word/'+target))
                        if not changed<=allowed:
                            raise HarnessError('preservation_failed','Field refresh changed an unexpected package part.')
                    for name in before.namelist():
                        if name not in changed and before.read(name)!=after.read(name):
                            raise HarnessError('preservation_failed','Patch changed an unrelated package part.')
            return result,candidate

    def list_objects(self, *, task_id=None, **arguments):
        request=ListObjects(**arguments)
        result=self._execute(request,'list')[0]
        from .evidence import issue
        return issue(self.store,task_id,{**result,'path':request.path,'working':bool(self.store.candidate(request.path))})

    def replace_text(self, *, idempotency_key, task_id=None, **arguments):
        batch=ReplaceTextCall(**arguments).as_batch()
        return self.stage_batch(**batch.model_dump(exclude={'tool'}),idempotency_key=idempotency_key,task_id=task_id)

    def format_text(self, *, idempotency_key, task_id=None, **arguments):
        batch=FormatTextCall(**arguments).as_batch()
        return self.stage_batch(**batch.model_dump(exclude={'tool'}),idempotency_key=idempotency_key,task_id=task_id)

    def find_text(self, *, task_id=None, **arguments):
        request=FindText(**arguments)
        result=self._execute(request,'find')[0]
        from .evidence import issue
        return issue(self.store,task_id,{**result,'path':request.path,'working':bool(self.store.candidate(request.path))})

    def read_objects(self, *, task_id=None, **arguments):
        request=ReadObjects(**arguments)
        result=self._execute(request,'read')[0]
        from .evidence import issue
        return issue(self.store,task_id,{**result,'path':request.path,'working':bool(self.store.candidate(request.path))})

    def annotate(self, **arguments):
        from ..annotations import annotate
        return annotate(self.store,**arguments)

    def plan_content(self, *, task_id, native_call_id=None, **arguments):
        from .content_plan import PlanContent,plan
        request=PlanContent(**arguments)  # Shape failures cannot become repair drafts.
        try:
            return plan(self.store,task_id,**arguments)
        except HarnessError as exc:
            if exc.code not in ('content_quote','content_ambiguous'):raise
            # plan() has left its transaction: capture only after its atomic rollback.
            from .content_plan_repair import capture,failure_result
            hint=capture(self.store,task_id,request.model_dump(),exc,native_call_id=native_call_id)
            if hint is not None:
                failure=failure_result(self.store,task_id,request.model_dump(),native_call_id=native_call_id)
                if failure is None:
                    raise HarnessError('state_corrupt','Captured planning failure has no durable receipt.') from exc
                raise HarnessError(failure['code'],failure['message'],
                                   recovery=failure['recovery']) from exc
            raise

    def select_source_quotes(self, *, task_id, native_call_id=None, **arguments):
        from .source_quotes import select
        return select(self.store,task_id,native_call_id=native_call_id,**arguments)

    def plan_requirements(self, *, task_id, native_call_id=None, **arguments):
        from .requirements import plan
        return plan(self.store,task_id,native_call_id=native_call_id,**arguments)

    def plan_draft(self, *, task_id, native_call_id=None, **arguments):
        from .draft_fragments import plan_draft
        return plan_draft(self.store,task_id,native_call_id=native_call_id,**arguments)

    def submit_draft(self, *, task_id, native_call_id=None, **arguments):
        from .draft_fragments import submit_draft
        return submit_draft(self.store,task_id,native_call_id=native_call_id,**arguments)

    def repair_content_plan(self, *, task_id, **arguments):
        from .content_plan_repair import repair
        return repair(self.store,task_id,**arguments)

    def load_tools(self, *, task_id, operations):
        from .guidance import load
        return load(self.store,task_id,operations)

    def export_pdf(self, **arguments):
        from .export import stage_pdf_export
        return stage_pdf_export(self,**arguments)

    def prepare_creation(self, *, task_id, native_call_id=None,
                         expected_page_requirement_stamp=_UNGATED_PAGE_REQUIREMENTS, **arguments):
        from .contracts import PrepareCreation
        from .creation_repair import capture,pending,get,_save
        from . import creation_submission as submission
        request=PrepareCreation(**arguments)
        payload=request.model_dump()
        payload['tool']='word.create'
        with self.store.transaction():
            if expected_page_requirement_stamp is not _UNGATED_PAGE_REQUIREMENTS:
                from .page_requirement_projection import require_fresh
                try:
                    require_fresh(self.store,task_id,expected_page_requirement_stamp)
                except HarnessError as exc:
                    raise PageRequirementRejected(exc.code,str(exc),recovery=exc.recovery) from exc
            if native_call_id is not None:
                prior=submission.result(self.store,task_id,native_call_id=native_call_id)
                if prior is not None:
                    # Check caller arguments too; historical replay never recaptures.
                    _,_,_,original=submission._call(self.store,task_id,native_call_id)
                    if canonical(original)!=canonical(payload):
                        raise HarnessError('native_history','Preparation replay has different arguments.')
                    return prior
            from .requirements import require_planned
            require_planned(self.store,task_id)
            verified=submission.verify(self.store,task_id,payload,native_call_id=native_call_id)
            receipt=capture(self.store,task_id,payload,allow_valid=True)
            state=get(self.store,task_id,receipt['ref'])
            if state.get('edit_effect_current') is True:
                state['edit_effect_current']=False
                _save(self.store,state)
                receipt=pending(self.store,task_id)
            submission.record(self.store,task_id,receipt['ref'],verified,receipt)
            return (submission.result(self.store,task_id,native_call_id=native_call_id)
                    if native_call_id is not None else pending(self.store,task_id))

    def revise_creation(self, *, task_id, **arguments):
        from .contracts import ReviseCreation
        from .creation_revision import revise
        request=ReviseCreation(**arguments)
        return revise(self.store,task_id,request.ref,[change.model_dump() for change in request.changes])

    def edit_creation_blocks(self, *, task_id, native_call_id=None, **arguments):
        from .creation_blocks import edit, normalize
        request=normalize(arguments)
        return edit(self.store,task_id,ref=request['ref'],changes=request['changes'],native_call_id=native_call_id)

    def edit_creation_styles(self, *, task_id, native_call_id=None, **arguments):
        from .creation_styles import edit, normalize
        request=normalize(arguments)
        return edit(self.store,task_id,ref=request['ref'],changes=request['changes'],native_call_id=native_call_id)

    def finalize_creation(self, *, idempotency_key, task_id, **arguments):
        from .contracts import FinalizeCreation
        return self._finalize_creation(FinalizeCreation(**arguments).model_dump(),idempotency_key=idempotency_key,task_id=task_id)

    def repair_creation(self, *, idempotency_key, task_id, **arguments):
        from .contracts import RepairCreation
        return self._finalize_creation(RepairCreation(**arguments).model_dump(),idempotency_key=idempotency_key,task_id=task_id)

    def _finalize_creation(self, call, *, idempotency_key, task_id):
        from .creation_repair import resolve,get
        from ..native_history import _owner
        with self.store.lock:
            _owner(self.store,task_id)
            previous=self.store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if previous:
                saved=json.loads(previous['args'])
                if digest(canonical(saved).encode())!=previous['args_hash']:
                    raise HarnessError('state_corrupt','Recorded repair arguments failed their hash check.')
                from .creation_identity import normalize_repair_identity
                if normalize_repair_identity(saved.get('repair_request'))!=normalize_repair_identity(call) or saved.get('task_id')!=task_id:
                    raise HarnessError('idempotency_conflict','Repair identity has different arguments.')
                return self._receipt(dict(previous))
            # Preserve the submitted list/map representation for exact ledger identity.
            # Convert only after replay lookup; resolver retains role/slot/source checks.
            source_bindings=({item['slot']:item['ref'] for item in call['bindings']}
                             if call['bindings'] else call['source_bindings'])
            request=resolve(self.store,task_id,call['ref'],call['styles'],source_bindings=source_bindings)
            return self._create(request,idempotency_key=idempotency_key,task_id=task_id,repair_request=call,repair_origin=get(self.store,task_id,call['ref']))

    def create(self, *, idempotency_key, task_id=None, **arguments):
        return self._create(CreateDocument(**arguments),idempotency_key=idempotency_key,task_id=task_id)

    def _create(self, request, *, idempotency_key, task_id=None, repair_request=None, repair_origin=None):
        submitted=request.model_dump() if request.literal_refs else request.model_dump(exclude={'literal_refs'})
        from .content_plan import has_bindings
        bound=has_bindings(request)
        with self.store.lock:
            if bound:
                from .content_plan import source
                source(self.store,task_id)  # Ownership check also applies to replays.
            previous=self.store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if previous:
                saved=json.loads(previous['args'])
                if digest(canonical(saved).encode())!=previous['args_hash']:
                    raise HarnessError('state_corrupt','Recorded operation arguments failed their hash check.')
                from .creation_identity import normalize_creation_identity
                if canonical(normalize_creation_identity(saved.get('submitted_request',saved['request'])))!=canonical(normalize_creation_identity(submitted)) or saved.get('task_id')!=task_id:
                    raise HarnessError('idempotency_conflict','Creation identity has different arguments.')
                return self._receipt(dict(previous))
            from .requirements import require_planned
            require_planned(self.store,task_id)
            if bound:
                from .content_plan import bind_creation
                request,bindings=bind_creation(self.store,task_id,request)
                args={'request':request.model_dump(exclude={'literal_refs'}),'task_id':task_id,'submitted_request':submitted,'literal_bindings':bindings}
            else:args={'request':submitted,'task_id':task_id}
            if repair_request is not None:
                args['repair_request']=repair_request
                args['repair_origin']={k:repair_origin[k] for k in ('ref','payload','payload_hash','objective_hash','sources','literal_bindings')}
                for key in ('resolved_source_bindings','resolved_source_plan'):
                    if key in repair_origin:args['repair_origin'][key]=repair_origin[key]
            from ..annotations import validate_word_scope
            validate_word_scope(self.store,task_id,request.path,None,[])
            if has_bindings(request):raise HarnessError('content_reference','Unresolved content references cannot reach the document worker.')
            requirement_evidence=None
            if task_id is not None:
                from .requirements import get as requirement_plan
                selected=requirement_plan(self.store,task_id)
                if selected is not None:
                    from .requirement_checks import require
                    checks=require(self.store,task_id,request)
                    requirement_evidence={
                        'generation':selected['generation'],
                        'plan_hash':digest(canonical(selected).encode()),
                        'resolved_proposal_hash':digest(canonical(request.model_dump()).encode()),
                        'checks':checks}
                    args['requirement_verification']=requirement_evidence
            result,data=self._execute(request,'create')
            from .creation_evidence import inspect as inspect_creation
            creation_evidence=inspect_creation(data)
            operation=self.store._stage_validated_bytes(path=request.path,data=data,expected_revision=None,
                idempotency_key=idempotency_key,task_id=task_id,tool='word.stage',args=args,
                validation={'openxml':True,'creation':True,'results':[],'creation_evidence':creation_evidence,
                            **({'requirement_verification':requirement_evidence} if requirement_evidence is not None else {}),
                            'warnings':['TOC field created; PDF previews update it, but saved DOCX field results still require an update.']
                            if any(b.kind=='table_of_contents' for b in request.blocks) else []})
            return self._receipt(operation)

    def refresh_fields(self, *, idempotency_key, task_id=None, **arguments):
        request=RefreshFields(**arguments)
        args={'request':request.model_dump(),'task_id':task_id}
        from ..annotations import validate_word_scope
        from .render import WordRenderer
        with self.store.lock:
            previous=self.store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if previous:
                if previous['args_hash']!=digest(canonical(args).encode()):
                    raise HarnessError('idempotency_conflict','Field refresh identity has different arguments.')
                return self._receipt(dict(previous))
            validate_word_scope(self.store,task_id,request.path,request.revision,[])
            self._bytes(request.path,request.revision)
        rendered=asyncio.run(WordRenderer(self.store).render(path=request.path,revision=request.revision,capture_fields=True))
        if not rendered.get('field_snapshot_hash'):
            raise HarnessError('field_update_unsupported','The document has no supported TOC or page fields to refresh.')
        snapshot=self.store.blob(rendered['field_snapshot_hash'])
        with self.store.lock:
            previous=self.store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if previous:
                if previous['args_hash']!=digest(canonical(args).encode()):
                    raise HarnessError('idempotency_conflict','Field refresh identity has different arguments.')
                return self._receipt(dict(previous))
            validate_word_scope(self.store,task_id,request.path,request.revision,[])
            result,data=self._execute(request,'refresh_fields',field_snapshot=snapshot)
            if result.get('unchanged'):
                current=self.store.candidate(request.path)
                return {'status':'unchanged','receipt_id':current['operation_id'] if current else None,
                        'input_revision':request.revision,'candidate_revision':request.revision,
                        'results':[],'message':'Saved field caches already match. No new operation.'}
            operation=self.store._stage_validated_bytes(path=request.path,data=data,expected_revision=request.revision,
                idempotency_key=idempotency_key,task_id=task_id,tool='word.stage',args=args,
                validation={'openxml':True,'preserved_unaffected_parts':True,'results':[],
                    'field_snapshot_hash':rendered['field_snapshot_hash'],'render_source_revision':request.revision,
                    'toc_refreshed':result.get('toc_refreshed',False),'page_fields_updated':result.get('page_fields_updated',0),
                    'warnings':['Saved field caches refreshed; review layout. Shared header/footer PAGE has one cached value and evaluates separately on each rendered page.']})
            return self._receipt(operation)

    def stage_batch(self, *, idempotency_key, task_id=None, **arguments):
        request=StageBatch(**arguments)
        submitted=request.model_dump()
        references=any(op.expected_hash.startswith('ev_') for op in request.operations)
        with self.store.lock:
            if references:
                from .evidence import owner,resolve
                owner(self.store,task_id)
            previous=self.store.db.execute('SELECT * FROM operations WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if previous:
                saved=json.loads(previous['args'])
                if digest(canonical(saved).encode())!=previous['args_hash']:
                    raise HarnessError('state_corrupt','Recorded operation arguments failed their hash check.')
                if canonical(saved.get('submitted_request',saved['request']))!=canonical(submitted) or saved.get('task_id')!=task_id:
                    raise HarnessError('idempotency_conflict','Batch identity already has different arguments.')
                return self._receipt(dict(previous))
            if references:
                request=request.model_copy(update={'operations':[
                    op.model_copy(update={'expected_hash':resolve(self.store,task_id,request.path,request.revision,op.target,op.expected_hash)})
                    if op.expected_hash.startswith('ev_') else op for op in request.operations]})
            args={'request':request.model_dump(),'task_id':task_id}
            if references:args['submitted_request']=submitted
            from ..annotations import validate_word_scope
            validate_word_scope(self.store,task_id,request.path,request.revision,request.operations)
            result,data=self._execute(request,'patch')
            if result.get('unchanged'):
                current=self.store.candidate(request.path)
                return {'status':'unchanged','receipt_id':current['operation_id'] if current else None,
                        'input_revision':request.revision,'candidate_revision':request.revision,
                        'results':result['results'],'message':'Requested values already match. No new operation; continue with remaining work or finish.'}
            receipt=self.store._stage_validated_bytes(path=request.path,data=data,expected_revision=request.revision,
                idempotency_key=idempotency_key,task_id=task_id,tool='word.stage',args=args,
                validation={'openxml':True,'preserved_unaffected_parts':True,'results':result['results']})
            with self.store.transaction():
                for op in request.operations:
                    if op.op in ('insert_image','replace_image'):
                        self.store.node('revision:'+receipt['candidate_hash'],'revision',{'path':request.path},receipt['candidate_hash'])
                        self.store.edge(op.asset_id,'revision:'+receipt['candidate_hash'],'placed-in','operation:'+receipt['id'])
            return self._receipt(receipt)

    def _receipt(self, operation):
        row=self.store.db.execute("SELECT detail FROM events WHERE operation_id=? AND state='validated' ORDER BY seq LIMIT 1",(operation['id'],)).fetchone()
        evidence=json.loads(row[0]) if row else {}
        verification={}
        if evidence.get('creation_evidence'):
            from .creation_evidence import compact
            verification={'creation_verification':compact(evidence['creation_evidence'])}
        return {**verification,**({'warnings':evidence['warnings']} if evidence.get('warnings') else {}),
                'receipt_id':operation['id'],'status':operation['status'],
                'input_revision':json.loads(operation['args'])['request']['revision'],
                'candidate_revision':operation['candidate_hash'],'results':evidence.get('results',[])}

    async def call_async(self, name, **arguments):
        # Pause waits for this bounded worker operation to settle. Shielding avoids
        # abandoning a background thread that could otherwise stage after Pause.
        if self.busy:raise HarnessError('word_busy','Another Word worker operation is in progress.')
        self.busy=True
        try:
            work=asyncio.create_task(asyncio.to_thread(getattr(self,name),**arguments))
            try:return await asyncio.shield(work)
            except asyncio.CancelledError:
                try:await work
                except Exception:pass
                raise
        finally:self.busy=False
