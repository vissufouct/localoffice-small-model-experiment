"""Revision-bound LibreOffice PDF rendering of managed copies only."""
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import zipfile
from xml.etree import ElementTree as ET
import psutil
from pypdf import PdfReader
from ..errors import HarnessError
from ..storage import canonical,digest
from ..worker_guard import guarded_command
from .contracts import Target
from .plugin import WordPlugin


def font_fingerprint():
    records=[]
    for root in (Path('/System/Library/Fonts'),Path('/Library/Fonts'),Path.home()/'Library/Fonts'):
        if not root.is_dir():continue
        for p in sorted(root.rglob('*')):
            if p.is_file():
                stat=p.stat();records.append((str(p),stat.st_size,stat.st_mtime_ns))
            if len(records)>20000:raise HarnessError('font_limit','Font inventory exceeds the render cache limit.')
    return digest(canonical(records).encode())


def preflight(data):
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        names=package.namelist()
        if len(names)>10000 or len(set(names))!=len(names):raise HarnessError('package_limit','Invalid or oversized package directory.')
        if sum(e.file_size for e in package.infolist())>200_000_000:raise HarnessError('package_limit','Expanded package exceeds 200 MB.')
        for name in names:
            if 'vbaproject' in name.lower() or '/activex/' in name.lower() or '/embeddings/' in name.lower():
                raise HarnessError('render_unsupported','Active or embedded objects require a qualified render adapter.')
            if name.endswith('.rels'):
                if package.getinfo(name).file_size>1_000_000:raise HarnessError('package_limit','Relationship part exceeds limit.')
                for rel in ET.fromstring(package.read(name)):
                    if rel.get('TargetMode')=='External' and not rel.get('Type','').endswith('/hyperlink'):
                        raise HarnessError('external_dependency','Rendering would require an external document resource.')


def field_warnings(data):
    from .fields import contains_toc
    return (['PDF preview recalculates the TOC; use refresh_fields when the saved DOCX TOC cache needs updating.']
            if contains_toc(data) else [])


class WordRenderer:
    def __init__(self,store):
        self.store=store;self.busy=False;self.process=None;self.cancel_requested=False
        self.executable=shutil.which('soffice')

    async def cancel(self):
        self.cancel_requested=True
        if self.process and self.process.returncode is None:
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        return {'status':'cancelling' if self.busy else 'idle'}

    async def render(self, *, path, revision, capture_fields=False):
        request=Target(path=path,revision=revision)
        if self.busy:raise HarnessError('render_busy','Another Word render is in progress.')
        if not self.executable:raise HarnessError('renderer_missing','LibreOffice is not installed.')
        if psutil.virtual_memory().available<1024**3:raise HarnessError('memory_admission','Word rendering needs at least 1 GiB available memory.')
        self.busy=True;self.cancel_requested=False
        try:
            data=WordPlugin(self.store)._bytes(request.path,request.revision);preflight(data)
            # Version is a short separate command, never a document load.
            version_process=await asyncio.create_subprocess_exec(self.executable,'--version',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            try:version_bytes,_=await asyncio.wait_for(version_process.communicate(),5)
            finally:
                if version_process.returncode is None:version_process.kill();await version_process.wait()
            version=version_bytes.decode().strip()
            from .fields import contains_toc,contains_page_fields,adapter
            field_command=None
            configuration={'renderer':version,'filter':'writer_pdf_Export','fonts':font_fingerprint(),'adapter_version':5}
            if contains_toc(data) or (capture_fields and contains_page_fields(data)):
                field_command,configuration['field_adapter']=adapter(data)
            cache_key=digest(canonical({'revision':revision,**configuration}).encode())
            cached=self.store.db.execute('SELECT body FROM render_cache WHERE key=?',(cache_key,)).fetchone()
            if cached:
                result=json.loads(cached[0]);self.store.blob(result['pdf_hash'])
                return {**result,'path':path,'cached':True}
            with tempfile.TemporaryDirectory(prefix='render-',dir=self.store.state) as directory:
                root=Path(directory);source=root/'source.docx';source.write_bytes(data)
                output=root/'output';output.mkdir();profile=root/'profile'
                command=guarded_command(*field_command,str(profile),str(source),
                                        str(output/'source.pdf'),str(output/'updated.docx')) if field_command else guarded_command(
                                        self.executable,'-env:UserInstallation='+profile.as_uri(),'--headless','--nologo','--nodefault','--norestore',
                                        '--convert-to','pdf:writer_pdf_Export','--outdir',str(output),str(source))
                self.process=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
                if self.cancel_requested:await self.cancel()
                try:stdout,stderr=await asyncio.wait_for(self.process.communicate(),60)
                except asyncio.CancelledError:
                    await self.cancel();await self.process.wait();raise
                except asyncio.TimeoutError as exc:
                    await self.cancel();await self.process.wait()
                    raise HarnessError('render_timeout','Word rendering exceeded 60 seconds.') from exc
                if self.cancel_requested:raise HarnessError('render_cancelled','Word rendering stopped.')
                pdf=output/'source.pdf'
                if self.process.returncode or not pdf.is_file():
                    raise HarnessError('render_failed','LibreOffice did not produce a PDF: '+stderr.decode(errors='replace')[-600:])
                field_receipt=None
                if field_command:
                    try:
                        field_receipt=json.loads(stdout)
                        if field_receipt['indexes_updated']<0 or (contains_toc(data) and field_receipt['indexes_updated']<1) or not 2<=field_receipt['update_passes']<=6:raise ValueError()
                    except (ValueError,KeyError,TypeError) as exc:
                        raise HarnessError('field_update_failed','Field update receipt was missing or invalid.') from exc
                if pdf.stat().st_size>50_000_000:raise HarnessError('render_limit','Rendered PDF exceeds 50 MB.')
                pdf_bytes=pdf.read_bytes();pages=len(PdfReader(io.BytesIO(pdf_bytes)).pages)
                if not 1<=pages<=2000:raise HarnessError('render_limit','Rendered PDF page count exceeds limits.')
                # A render of an old revision is not labelled as the current preview.
                WordPlugin(self.store)._bytes(path,revision)
                pdf_hash=self.store.put_blob(pdf_bytes)
                result={'path':path,'revision':revision,'pdf_hash':pdf_hash,'pages':pages,'configuration':configuration,
                        'warnings':['LibreOffice rendering; minor Microsoft Word differences are accepted.','Font substitution is not yet automatically diagnosed.']+field_warnings(data),'cached':False}
                if field_receipt:
                    snapshot=output/'updated.docx'
                    if not snapshot.is_file() or snapshot.stat().st_size>50_000_000:
                        raise HarnessError('field_update_failed','Updated field snapshot missing or exceeds size limit.')
                    snapshot_bytes=snapshot.read_bytes();preflight(snapshot_bytes)
                    result['field_snapshot_hash']=self.store.put_blob(snapshot_bytes)
                    result['field_update']=field_receipt
                with self.store.transaction():
                    self.store.db.execute('INSERT OR REPLACE INTO render_cache VALUES(?,?,?)',(cache_key,revision,canonical(result)))
                return result
        except asyncio.CancelledError:
            await self.cancel();raise
        finally:
            if self.process and self.process.returncode is None:
                await self.cancel();await self.process.wait()
            self.process=None;self.busy=False
