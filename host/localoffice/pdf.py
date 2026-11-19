"""Source snapshots, bounded subprocess work and page-level provenance."""
import asyncio
import base64
import json
import os
import signal
import sys
import time
from pathlib import Path
import psutil
from .errors import HarnessError
from .paths import project_path
from .storage import canonical,digest
from .worker_guard import guarded_command


class PdfPlugin:
    def __init__(self,store):
        self.store=store;self.busy=False;self.process=None;self.cancel_requested=False
        self.tessdata=Path.home()/'.localoffice/tessdata'

    async def cancel(self):
        if not self.busy:return {'status':'idle'}
        self.cancel_requested=True
        process=self.process
        if process and process.returncode is None:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        return {'status':'cancelling'}

    async def job(self,path,*,command='read',page=1,count=1,ocr=False,languages='eng+por',password=None,expected_revision=None):
        if self.busy:raise HarnessError('pdf_busy','Another PDF/OCR job is in progress.')
        if command not in ('read','render') or type(page) is not int or page<1 or type(count) is not int or not 1<=count<=3:
            raise HarnessError('pdf_arguments','Use a positive page and a count from one to three.')
        if languages not in ('eng','por','eng+por'):
            raise HarnessError('ocr_language','Initial OCR languages are English and Portuguese.')
        source=project_path(self.store.root,path,must_exist=True)
        if source.suffix.lower()!='.pdf':raise HarnessError('format_unsupported','Choose a PDF source.')
        if source.stat().st_size>50_000_000:raise HarnessError('file_limit','Initial PDF limit is 50 MB.')
        if psutil.virtual_memory().available<512*2**20:raise HarnessError('memory_admission','PDF work needs at least 512 MiB available memory.')
        if ocr:
            missing=[lang for lang in languages.split('+') if not (self.tessdata/(lang+'.traineddata')).is_file()]
            if missing:raise HarnessError('ocr_data_missing','Install approved OCR data for: '+', '.join(missing))
        self.busy=True;self.cancel_requested=False
        process=None;communication=None
        try:
            data=source.read_bytes()
            if len(data)>50_000_000:raise HarnessError('file_limit','PDF grew beyond the source limit.')
            revision=digest(data)
            if expected_revision is not None and revision!=expected_revision:
                raise HarnessError('stale_revision','PDF changed since preview. Reopen it before extracting text.')
            self.store.put_blob(data)
            request={'snapshot':str(self.store.blobs/revision),'command':command,'page':page,'count':count,
                     'ocr':ocr,'languages':languages,'tessdata':str(self.tessdata),'password':password}
            process=await asyncio.create_subprocess_exec(*guarded_command(sys.executable,'-m','localoffice.pdf_worker'),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,start_new_session=True,limit=16_000_000)
            self.process=process
            if self.cancel_requested:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            communication=asyncio.create_task(process.communicate((json.dumps(request)+'\n').encode()))
            started=time.monotonic();peak=0
            while not communication.done():
                await asyncio.sleep(.1)
                try:
                    parent=psutil.Process(process.pid)
                    rss=parent.memory_info().rss+sum(p.memory_info().rss for p in parent.children(recursive=True) if p.is_running())
                    peak=max(peak,rss)
                    if rss>768*2**20:raise HarnessError('pdf_memory_limit','PDF worker exceeded its 768 MiB resident-memory limit.')
                except psutil.NoSuchProcess:pass
                if time.monotonic()-started>90:raise HarnessError('pdf_timeout','PDF job exceeded its 90-second limit.')
            out,_=await communication
            if self.cancel_requested:raise HarnessError('pdf_cancelled','PDF reading was cancelled. Source files are unchanged.')
            if len(out)>16_000_000:raise HarnessError('pdf_output_limit','Rendered PDF result exceeded the output limit.')
            result=json.loads(out)
            if result.get('error'):raise HarnessError(result['error'],result['message'])
            result.update(path=path,source_hash=revision,peak_sampled_rss_bytes=peak)
            if command=='read':
                for item in result['pages']:
                    item['citation']=f'[{path}, p. {item["page"]}, revision {revision[:12]}]'
                result['text']='\n\n'.join(item['citation']+'\n'+item['text'] for item in result['pages'])
            else:
                png=base64.b64decode(result.pop('png'),validate=True)
                result['image_hash']=self.store.put_blob(png)
                result['mime_type']='image/png'
            with self.store.transaction():
                self.store.node('file:'+path,'source',{'path':path},revision)
                self.store.node('revision:'+revision,'revision',{'path':path},revision)
                self.store.edge('file:'+path,'revision:'+revision,'contains','pdf.snapshot')
                for viewed_page in ([item['page'] for item in result['pages']] if command=='read' else [page]):
                    self.store.node(f'pdf:{revision}:{viewed_page}:{command}','source_view',{'path':path,'page':viewed_page,'kind':command},revision)
            return result
        finally:
            if process and process.returncode is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                await process.wait()
            if communication and not communication.done():
                communication.cancel()
                try:await communication
                except asyncio.CancelledError:pass
            self.process=None;self.busy=False

    async def read(self,path,page=1,count=1,ocr=False,languages='eng+por'):
        return await self.job(path,page=page,count=count,ocr=ocr,languages=languages)
