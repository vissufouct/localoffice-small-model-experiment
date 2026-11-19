from __future__ import annotations
import asyncio
import json
import os
import secrets
import socket
import time
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Literal
from starlette.background import BackgroundTask
from pydantic import BaseModel,ConfigDict,Field
import uvicorn
from .discovery import discover,hardware
from .errors import HarnessError
from .paths import project_path
from .providers import LlamaProvider,MLXProvider
from .runtime import TaskRunner
from .annotations import annotate as record_annotation
from .projects import ProjectRegistry
from .attachments import import_attachment
from .chats import create_chat,select_chat,current_chat,list_chats
from .storage import ProjectStore,canonical,digest
from .word.contracts import Target as WordTarget, ListObjects, ReadObjects


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')
class OpenProject(Body):path:str
class ProjectCreate(Body):
    name:str=Field(min_length=1,max_length=120)
    path:str
    create_folder:bool=False
class ChatCreate(Body):title:str=Field(default='New chat',min_length=1,max_length=120)
class ModelLoad(Body):path:str;context:int=4096;backend:str='llamacpp'
class Prompt(Body):online_research:bool|None=None;skill_id:str|None=None;text:str=Field(min_length=1,max_length=20000);mode:str='live';artifact:str|None=None;task_id:str|None=None;annotation_ids:list[str]=Field(default_factory=list,max_length=16)
class Apply(Body):operation_id:str;candidate_hash:str;action:str
class WordExport(WordTarget):
    pdf_hash:str=Field(pattern=r'^[a-f0-9]{64}$')
    destination:str=Field(min_length=1,max_length=1024)
    idempotency_key:str=Field(min_length=1,max_length=160)
class Annotation(Body):path:str;revision:str;scope:str;locator:dict;comment:str=Field(min_length=1,max_length=4000)
class PdfRequest(Body):
    path:str
    expected_revision:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    page:int=Field(default=1,ge=1,le=2000)
    count:int=Field(default=1,ge=1,le=3)
    ocr:bool=False
    languages:str='eng+por'
    password:str|None=Field(default=None,repr=False,max_length=1000)
class ReviewView(Body):
    pdf_hash:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    renderer:Literal['PDF.js','marked+DOMPurify']
    version:str=Field(min_length=1,max_length=80)
    page:int|None=Field(default=None,ge=1,le=2000)
    zoom:float=Field(default=1,ge=.1,le=5)
    scroll_top:float=Field(default=0,ge=0,le=10000000)
    viewport_height:float=Field(ge=1,le=10000)
class UserReview(Body):
    path:str
    revision:str=Field(pattern=r'^[a-f0-9]{64}$')
    phase:Literal['before','during','final']
    outcome:Literal['accepted','needs_changes']
    note:str=Field(default='',max_length=4000)
    view:ReviewView
class ForgetMemory(Body):confirmation:str
class SkillDraft(Body):
    name:str=Field(min_length=1,max_length=80)
    description:str=Field(min_length=1,max_length=1000)
    body:str=Field(min_length=1,max_length=12000)
    required_tools:list[str]=Field(default_factory=list,max_length=12)
class Decision(Body):
    content:str=Field(min_length=1,max_length=4000)
    source_message:int
    decision_id:str|None=None
    scope:Literal["chat","project"]="chat"
    expected_version:int|None=Field(default=None,ge=1)


class Host:
    def __init__(self):
        self.store=None;self.runner=None;self.provider=LlamaProvider();self.loading=False;self.importing=False
        self.settings=Path(os.environ['LOCALOFFICE_SETTINGS_DIR']).expanduser().resolve() if os.environ.get('LOCALOFFICE_SETTINGS_DIR') else Path.home()/'.localoffice-experiment'
        self.settings.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.projects=ProjectRegistry(self.settings)
    def project(self):
        if not self.store:raise HarnessError('no_project','Open a local folder first.')
        from .memory import finish_forget
        finish_forget(self.store)
        return self.store
    async def close(self):
        if self.runner:
            await self.runner.registry.word_renderer.cancel()
            while self.runner.registry.word_renderer.busy:await asyncio.sleep(.05)
            await self.runner.pause()
            while self.runner.registry.word.busy:await asyncio.sleep(.05)
        await self.provider.close()
        if self.store:self.store.close()


def create_app(token:str,host:Host|None=None):
    host=host or Host()
    @asynccontextmanager
    async def lifespan(app):
        yield
        await host.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.host=host
    app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:1420','http://localhost:1420','tauri://localhost','http://tauri.localhost'],allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'])
    @app.middleware('http')
    async def auth(request:Request,call_next):
        if request.method!='OPTIONS' and not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token):
            return JSONResponse({'code':'unauthorized','message':'Session authentication required.'},status_code=401)
        size=request.headers.get('content-length')
        limit=50_000_000 if request.url.path=='/attachments' else 1_000_000
        try:
            if size and int(size)>limit:return JSONResponse({'message':'Request too large'},status_code=413)
        except ValueError:return JSONResponse({'message':'Invalid Content-Length'},status_code=400)
        return await call_next(request)
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,exc):
        # Pydantic's default response includes raw input, including PDF passwords.
        return JSONResponse({'code':'invalid_request','message':'Invalid request fields. Check required values and limits.','errors':[{'type':item['type']} for item in exc.errors()]},status_code=422)
    @app.exception_handler(HarnessError)
    async def error(request,exc):return JSONResponse({'code':exc.code,'message':str(exc)},status_code=409)
    @app.exception_handler(FileNotFoundError)
    async def missing(request,exc):return JSONResponse({'code':'not_found','message':'Local file not found.'},status_code=404)

    @app.get('/health')
    async def health():return {'status':'ready','version':'0.1.0','api':1}
    @app.get('/hardware')
    async def hardware_route():return await asyncio.to_thread(hardware)
    @app.get('/models')
    async def models(path:str|None=None):return await asyncio.to_thread(discover,[path] if path else [])
    @app.get('/model')
    async def model():return {**host.provider.status(),'loading':host.loading}
    @app.post('/model/load')
    async def load(body:ModelLoad):
        if host.loading:raise HarnessError('model_busy','A model is already loading.')
        if host.runner and (host.runner.registry.pdf.busy or host.runner.registry.word_renderer.busy or host.runner.registry.word.busy):raise HarnessError('pdf_busy','Finish or cancel PDF/Word rendering before loading a model.')
        if body.backend not in ['llamacpp','mlx']:raise HarnessError('backend_unavailable','Choose llama.cpp or MLX.')
        host.loading=True
        try:
            if host.runner:await host.runner.pause()
            if host.provider.id!=body.backend:
                await host.provider.close()
                host.provider=LlamaProvider() if body.backend=='llamacpp' else MLXProvider()
                if host.runner:host.runner.provider=host.provider
            return await host.provider.start(body.path,body.context)
        finally:host.loading=False
    @app.post('/model/unload')
    async def unload():
        if host.loading:raise HarnessError('model_busy','Wait for the current load to settle.')
        if host.runner:await host.runner.pause()
        await host.provider.stop();return host.provider.status()
    @app.get('/preferences')
    async def preferences():
        p=host.settings/'preferences.json'
        return json.loads(p.read_text()) if p.exists() else {}
    @app.post('/project/open')
    async def open_project(body:OpenProject):
        if host.importing:raise HarnessError('input_busy','Wait for the current file attachment to finish.')
        if host.runner and (host.runner.registry.pdf.busy or host.runner.registry.word_renderer.busy or host.runner.registry.word.busy):raise HarnessError('pdf_busy','Wait for the current PDF/Word render job to finish before switching projects or chats.')
        if host.store and Path(body.path).expanduser().resolve()==host.store.root:
            return host.store.snapshot()
        candidate=ProjectStore(body.path)
        if host.runner:await host.runner.pause()
        if host.store:host.store.close()
        host.store=candidate;host.runner=TaskRunner(candidate,host.provider)
        host.projects.register(candidate.root)
        from .storage import atomic_write
        atomic_write(host.settings/'preferences.json',canonical({'last_project':str(candidate.root)}).encode())
        return candidate.snapshot()
    @app.get('/projects')
    async def projects():return host.projects.listing()
    @app.post('/projects')
    async def project_create(body:ProjectCreate):
        root=Path(body.path).expanduser()
        if body.create_folder:root.mkdir(parents=True,exist_ok=True)
        if not root.is_dir():raise HarnessError('not_found','Choose an existing folder or enable Create folder.')
        result=await open_project(OpenProject(path=str(root)))
        host.projects.register(root,body.name)
        return result
    @app.get('/chats')
    async def chats():return list_chats(host.project())
    @app.post('/chats')
    async def chat_create(body:ChatCreate):
        if host.importing:raise HarnessError('input_busy','Wait for the current file attachment to finish.')
        if host.runner and (host.runner.registry.pdf.busy or host.runner.registry.word_renderer.busy or host.runner.registry.word.busy):raise HarnessError('pdf_busy','Wait for the current PDF/Word render job to finish before switching projects or chats.')
        store=host.project();await host.runner.pause()
        key=create_chat(store,body.title);select_chat(store,key)
        host.runner=TaskRunner(store,host.provider)
        return store.snapshot()
    @app.post('/chats/{chat_id}/open')
    async def chat_open(chat_id:str):
        if host.importing:raise HarnessError('input_busy','Wait for the current file attachment to finish.')
        if host.runner and (host.runner.registry.pdf.busy or host.runner.registry.word_renderer.busy or host.runner.registry.word.busy):raise HarnessError('pdf_busy','Wait for the current PDF/Word render job to finish before switching projects or chats.')
        store=host.project()
        if chat_id==current_chat(store):return store.snapshot()
        if not store.db.execute('SELECT 1 FROM chats WHERE id=?',(chat_id,)).fetchone():raise HarnessError('not_found','Chat not found in this project.')
        await host.runner.pause();select_chat(store,chat_id)
        host.runner=TaskRunner(store,host.provider)
        row=store.db.execute("SELECT t.id FROM tasks t JOIN task_chats c ON c.task_id=t.id WHERE c.chat_id=? AND t.status IN ('paused','failed') ORDER BY t.updated DESC LIMIT 1",(chat_id,)).fetchone()
        host.runner.active=row[0] if row else None
        return store.snapshot()
    @app.post('/attachments')
    async def attach(request:Request,filename:str):
        store=host.project()
        if host.importing:raise HarnessError('input_busy','Another attachment is being imported.')
        host.importing=True
        temporary=None
        try:
            folder=store.state/'imports';folder.mkdir(exist_ok=True)
            if folder.is_symlink():raise HarnessError('path_denied','Import staging must not be a symbolic link.')
            with tempfile.NamedTemporaryFile(dir=folder,delete=False) as output:
                temporary=Path(output.name);size=0
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>50_000_000:raise HarnessError('attachment_limit','Attachment exceeds 50 MB.')
                    output.write(chunk)
            return import_attachment(store,filename,temporary.read_bytes())
        finally:
            if temporary:temporary.unlink(missing_ok=True)
            host.importing=False
    @app.get('/attachments')
    async def attachments():
        store=host.project()
        return [dict(row) for row in store.db.execute('SELECT * FROM attachments WHERE chat_id=? ORDER BY created',(current_chat(store),))]
    @app.get('/project')
    async def project():return host.project().snapshot()
    @app.get('/files')
    async def files():
        store=host.project();result=[]
        for base,dirs,names in os.walk(store.root,followlinks=False):
            dirs[:]=sorted(d for d in dirs if not d.startswith('.') and d not in ['node_modules','target','dist','__pycache__'] and not (Path(base)/d).is_symlink())
            for name in sorted(names):
                p=Path(base)/name
                if name.startswith('.') or p.is_symlink():continue
                result.append({'path':str(p.relative_to(store.root)),'bytes':p.stat().st_size})
                if len(result)>=2000:return {'files':result,'limited':True}
        return {'files':result,'limited':False}
    @app.get('/file')
    async def file(path:str,working:bool=True):return host.project().read(path,working=working)
    @app.get('/file/revision')
    async def file_revision(path:str):
        store=host.project()
        source=project_path(store.root,path)
        if source.is_file() and source.stat().st_size>50_000_000:
            raise HarnessError('file_limit','Preview monitoring supports files up to 50 MB.')
        pending=store.candidate(path)
        try:
            original=await asyncio.to_thread(store.file_hash,path)
        except FileNotFoundError:
            original=None
        return {'original_revision':original,
                'preview_revision':pending['candidate_hash'] if pending else original,
                'conflict':bool(pending and pending['base_hash']!=original)}

    @app.get('/binary')
    async def binary(path:str):
        store=host.project();p=project_path(store.root,path)
        if p.suffix.lower()!='.pdf':raise HarnessError('format_unsupported','This endpoint serves PDF sources only.')
        pending=store.candidate(path)
        if pending:return Response(store.blob(pending['candidate_hash']),media_type='application/pdf')
        p=project_path(store.root,path,must_exist=True)
        if p.stat().st_size>50_000_000:raise HarnessError('file_limit','Initial PDF limit is 50 MB.')
        return Response(p.read_bytes(),media_type='application/pdf')
    @app.get('/citation')
    async def citation(path:str,page:int,revision:str):
        import re
        store=host.project()
        project_path(store.root,path,must_exist=True)
        if page<1 or page>2000 or not re.fullmatch('[a-f0-9]{12}',revision):
            raise HarnessError('citation_invalid','Citation page or revision is invalid.')
        rows=store.db.execute("SELECT DISTINCT revision FROM nodes WHERE kind='source_view' AND substr(revision,1,12)=? AND json_extract(body,'$.path')=? AND json_extract(body,'$.page')=? AND json_extract(body,'$.kind')='read'",(revision,path,page)).fetchall()
        if len(rows)!=1:raise HarnessError('citation_unverified','No unique extracted source page matches this citation.')
        exact=rows[0]['revision']
        if store.file_hash(path)!=exact:raise HarnessError('citation_stale','The cited PDF has changed. This citation refers to an earlier revision.')
        return {'path':path,'page':page,'revision':exact,'verified':'source identity and extracted page; not semantic claim support'}
    def admit_manual_pdf():
        host.project()
        if host.loading:raise HarnessError('model_busy','Wait for model loading before reading or rendering pages.')
        if host.runner.registry.word.busy:raise HarnessError('word_busy','Finish the current Word operation first.')
        if host.runner.registry.word_renderer.busy:raise HarnessError('render_busy','Finish or cancel the current Word render first.')
        if host.runner.state()['running']:raise HarnessError('task_busy','Pause the active task before manual PDF reading or rendering.')

    def admit_manual_word():
        admit_manual_pdf()
        if host.runner.registry.pdf.busy:raise HarnessError('pdf_busy','Finish or cancel PDF reading first.')
        if not host.runner.registry.enabled('word'):raise HarnessError('plugin_disabled','Word plugin is disabled.')

    @app.post('/word/new-target')
    async def word_new_target(body:OpenProject):
        admit_manual_word()
        store=host.project()
        if len(body.path)>1024 or '\\' in body.path or '\x00' in body.path:
            raise HarnessError('path_denied','Use a project-relative DOCX path of at most 1,024 characters.')
        target=project_path(store.root,body.path)
        path=target.relative_to(store.root).as_posix()
        if target.suffix.lower()!='.docx':
            raise HarnessError('format_unsupported','Choose a .docx filename for the new Word document.')
        if target.exists() or store.candidate(path):
            raise HarnessError('target_exists','This filename already has a file or working copy. Open it to edit, or choose another name.')
        active=await host.runner.activate_artifact(path)
        return {'path':path,'active':active,'created':False}

    @app.post('/word/objects')
    async def word_objects(body:ListObjects):
        admit_manual_word()
        return await host.runner.registry.call('word.list_objects',**body.model_dump(exclude={'tool'}))

    @app.post('/word/read')
    async def word_read(body:ReadObjects):
        admit_manual_word()
        return await host.runner.registry.call('word.read_objects',**body.model_dump(exclude={'tool'}))

    @app.post('/word/render')
    async def word_render(body:WordTarget):
        admit_manual_word()
        return await host.runner.registry.word_renderer.render(**body.model_dump())

    @app.post('/word/render/cancel')
    async def word_render_cancel():
        host.project()
        return await host.runner.registry.word_renderer.cancel()

    @app.post('/word/export-pdf')
    async def export_word_pdf(body:WordExport):
        admit_manual_word()
        return await host.runner.registry.word.call_async('export_pdf',**body.model_dump())

    @app.get('/word/pdf/{pdf_hash}')
    async def word_pdf(pdf_hash:str,path:str,revision:str):
        store=host.project()
        request=WordTarget(path=path,revision=revision)
        host.runner.registry.word._bytes(request.path,request.revision)
        rows=store.db.execute('SELECT body FROM render_cache WHERE revision=?',(revision,)).fetchall()
        if not any(json.loads(row[0]).get('pdf_hash')==pdf_hash for row in rows):
            raise HarnessError('not_found','No rendered PDF for this source revision.')
        data=store.blob(pdf_hash)
        if not data.startswith(b'%PDF-'):raise HarnessError('render_failed','Cached output is not a PDF.')
        return Response(data,media_type='application/pdf')

    @app.post('/pdf/read')
    async def pdf_read(body:PdfRequest):
        admit_manual_pdf()
        return await host.runner.registry.pdf.job(**body.model_dump())
    @app.post('/pdf/cancel')
    async def pdf_cancel():
        host.project()
        return await host.runner.registry.pdf.cancel()
    @app.post('/pdf/render')
    async def pdf_render(body:PdfRequest):
        admit_manual_pdf()
        result=await host.runner.registry.pdf.job(command='render',**body.model_dump())
        return result
    @app.post('/review/user')
    async def user_review(body:UserReview):
        store=host.project()
        if host.runner.state()['running']:
            raise HarnessError('task_busy','Pause before recording a visual review.')
        from .reviews import record_review
        return record_review(store,**{**body.model_dump(),'view':body.view.model_dump()})
    @app.get('/reviews')
    async def reviews(path:str):
        from .reviews import list_reviews
        return list_reviews(host.project(),path)
    @app.get('/view/{image_hash}')
    async def view_image(image_hash:str):
        store=host.project()
        data=store.blob(image_hash)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise HarnessError('format_unsupported','Only rendered PNG views are available here.')
        return Response(data,media_type='image/png')
    @app.post('/task')
    async def task(body:Prompt):
        host.project()
        if (host.runner.registry.pdf.busy or host.runner.registry.word_renderer.busy or host.runner.registry.word.busy):raise HarnessError('pdf_busy','Finish or cancel PDF/Word rendering before starting a task.')
        if host.loading:raise HarnessError('model_busy','Wait for the current model load to finish.')
        if body.task_id and not host.provider.status()['loaded'] and host.provider.status().get('resumable'):
            host.loading=True
            try:await host.provider.resume()
            finally:host.loading=False
        return {'task_id':await host.runner.start(body.text,body.mode,body.artifact,body.task_id,body.annotation_ids,body.skill_id,body.online_research)}
    @app.post('/artifact/select')
    async def select_artifact(body:OpenProject):
        host.project()
        return {'active':await host.runner.activate_artifact(body.path)}
    @app.get('/task/{task_id}/policy')
    async def policy(task_id:str):
        from .policy import task_policy
        return task_policy(host.project(),task_id)

    @app.get('/activity')
    async def activity():return {**(host.runner.state() if host.runner else {'events':[],'running':False,'active':None,'context':{}}),'model_status':{**host.provider.status(),'loading':host.loading}}
    @app.post('/pause')
    async def pause():
        host.project();await host.runner.pause();return {'status':'paused'}
    @app.post('/cancel')
    async def cancel():
        host.project();await host.runner.pause(cancel=True);return {'status':'cancelled'}
    @app.post('/apply')
    async def apply(body:Apply):
        host.project()
        if host.runner.state()['running']:raise HarnessError('task_busy','Pause before applying a working copy.')
        return host.store.apply(body.operation_id,expected_candidate=body.candidate_hash,action=body.action)
    @app.post('/undo/{operation_id}')
    async def undo(operation_id:str):
        host.project()
        if host.runner.state()['running']:raise HarnessError('task_busy','Pause before undo.')
        return host.store.undo(operation_id,str(uuid.uuid4()))
    @app.post('/annotation')
    async def annotate(body:Annotation):
        store=host.project()
        if host.runner.state()['running']:raise HarnessError('task_busy','Pause before annotating.')
        if body.path.lower().endswith('.docx'):
            admit_manual_word()
            key=await host.runner.registry.word.call_async('annotate',**body.model_dump())
        else:key=record_annotation(store,**body.model_dump())
        return {'id':key}
    @app.get('/plugins')
    async def plugins():
        host.project();return host.runner.registry.catalogue()
    @app.post('/plugins/{key}/{enabled}')
    async def plugin_toggle(key:str,enabled:bool):
        host.project();await host.runner.pause()
        host.runner.registry.set_enabled(key,enabled)
        return host.runner.registry.catalogue()
    @app.get('/skills')
    async def skills():
        host.project();return host.runner.skills.discover()
    @app.get('/skills/{key}')
    async def skill(key:str):
        host.project();return host.runner.skills.load(key)
    @app.post('/skills/draft')
    async def skill_draft(body:SkillDraft):
        host.project();return host.runner.skills.create_draft(**body.model_dump())
    @app.post('/skills/import')
    async def skill_import(body:OpenProject):
        host.project();return host.runner.skills.import_local(body.path)
    @app.get('/graph')
    async def graph(seed:str,depth:int=2):
        from .graph import neighborhood
        return neighborhood(host.project(),seed,depth=depth)
    @app.get('/history/search')
    async def search(q:str):return host.project().search(q)
    @app.get('/history/window')
    async def history_window(message_id:int,radius:int=2):return host.project().history_window(message_id,radius)

    @app.post('/decision')
    async def decision(body:Decision):
        store=host.project()
        if host.runner.state()['running']:raise HarnessError('task_busy','Pause before changing confirmed decisions.')
        if body.decision_id and body.expected_version is None:raise HarnessError('decision_version','Corrections require the version you reviewed.')
        return {'id':store.decision(**body.model_dump())}
    @app.get('/decisions')
    async def decision_list():
        store=host.project();chat=current_chat(store)
        rows=[dict(r) for r in store.db.execute("SELECT d.*,s.scope FROM decisions d JOIN decision_scopes s ON s.decision_id=d.id WHERE d.active=1 AND (s.scope='project' OR s.chat_id=?) ORDER BY d.rowid DESC",(chat,))]
        for row in rows:
            row['versions']=[dict(v) for v in store.db.execute("SELECT version,content,source_message,scope,created FROM decision_versions WHERE id=? AND (scope='project' OR chat_id=?) ORDER BY version DESC",(row['id'],chat))]
        return rows
    @app.post('/context/compact')
    async def compact():
        store=host.project()
        if host.runner.state()['running']:await host.runner.pause()
        return store.checkpoint(host.runner.active,summary_error='user requested deterministic compaction')
    @app.post('/context/fresh')
    async def fresh():
        host.project();await host.runner.pause();host.runner.active=None
        from .memory import start_fresh
        return {'status':'ready',**start_fresh(host.project())}
    @app.get('/removed-artifacts')
    async def removed_artifacts():
        store=host.project()
        return [dict(row) for row in store.db.execute("SELECT id,path,backup_hash FROM operations o WHERE tool='artifact.remove' AND status='committed' AND backup_hash IS NOT NULL AND rowid=(SELECT max(rowid) FROM operations WHERE path=o.path AND status='committed') ORDER BY created DESC LIMIT 100") if store.file_hash(row['path']) is None]
    @app.post('/memory/forget')
    async def forget_memory(body:ForgetMemory):
        store=host.project()
        if host.importing or (host.runner.registry.pdf.busy or host.runner.registry.word_renderer.busy or host.runner.registry.word.busy):
            raise HarnessError('memory_busy','Finish the current import or PDF job before forgetting memory.')
        await host.runner.pause()
        from .memory import forget_project_memory
        if body.confirmation!='FORGET PROJECT MEMORY':raise HarnessError('confirmation_required','Type FORGET PROJECT MEMORY to confirm permanent memory deletion.')
        await host.provider.stop()
        result=forget_project_memory(store,body.confirmation)
        host.runner=TaskRunner(store,host.provider)
        return result
    @app.get('/memory/export')
    async def export():
        from .memory import export_memory
        path=export_memory(host.project())
        return FileResponse(path,media_type='application/json',filename='localoffice-memory.json',background=BackgroundTask(path.unlink,missing_ok=True))
    return app


def main():
    token=secrets.token_urlsafe(40)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    port=sock.getsockname()[1]
    print(json.dumps({'url':f'http://127.0.0.1:{port}','token':token}),flush=True)
    app=create_app(token)
    config=uvicorn.Config(app,log_level='warning',access_log=False)
    server=uvicorn.Server(config)
    async def run():
        parent=os.getppid()
        async def monitor_parent():
            while not server.should_exit:
                await asyncio.sleep(1)
                if os.getppid()!=parent:server.should_exit=True
        monitor=asyncio.create_task(monitor_parent())
        try:await server.serve(sockets=[sock])
        finally:monitor.cancel()
    asyncio.run(run())

if __name__=='__main__':main()
