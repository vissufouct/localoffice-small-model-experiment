"""Batch-boundary scheduling: real Word mutations, controlled renderer/provider."""
import asyncio
import json
import io
from pypdf import PdfWriter
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.metrics import summary
from localoffice.errors import HarnessError
from test_word_creation import design


class CreationProvider:
    context_limit=4096
    def __init__(self):self.calls=0;self.prompts=[]
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 500
    async def stream(self,messages,**kwargs):
        self.calls+=1;self.prompts.append(json.dumps(messages))
        assert ('CreateDocument' in kwargs['schema'].get('$defs',{}))==(self.calls==1)
        assert 'FormatTable' not in kwargs['schema'].get('$defs',{})
        action={'tool':'word.create',**design()} if self.calls==1 else {'tool':'finish','text':'Review the staged report.'}
        yield {'type':'token','text':json.dumps(action)}
        yield {'type':'usage','usage':{'prompt_tokens':500,'completion_tokens':100}}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,outcome',[('live','success'),('live','failure'),('background','success')])
async def test_render_is_between_batches_without_model_roundtrip(tmp_path,monkeypatch,mode,outcome):
    store=ProjectStore(tmp_path);provider=CreationProvider();runner=TaskRunner(store,provider)
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def render(**kwargs):
        calls.append(kwargs);entered.set();await release.wait()
        if outcome=='failure':raise HarnessError('render_failed','controlled-render-failure')
        pdf=PdfWriter();pdf.add_blank_page(width=595,height=842);pdf.add_blank_page(width=595,height=842)
        output=io.BytesIO();pdf.write(output)
        return {**kwargs,'pdf_hash':store.put_blob(output.getvalue()),'cached':False,'pages':2,
                'configuration':{'renderer':'LibreOffice test','filter':'writer_pdf_Export','fonts':'b'*64,'adapter_version':5},'warnings':[]}
    monkeypatch.setattr(runner.registry.word_renderer,'render',render)
    try:
        task=await runner.start('Create a report',mode=mode,artifact='report.docx')
        if mode=='live':
            await asyncio.wait_for(entered.wait(),5)
            assert provider.calls==1  # No concurrent inference while rendering.
            candidate=store.candidate('report.docx');assert candidate
            assert store.db.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0]==1
            assert not (tmp_path/'report.docx').exists()
            release.set()
        await asyncio.wait_for(runner.job,5)
        assert provider.calls==2
        assert ('current_render' in provider.prompts[-1])==(mode=='live' and outcome=='success')
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        assert len(calls)==(1 if mode=='live' else 0)
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        events=[e['type'] for e in runner.events]
        if mode=='live':
            assert events.index('tool')<events.index('rendering')
            expected='candidate' if outcome=='success' else 'preview_error'
            assert events.index('rendering')<events.index(expected)<events.index('completed')
        else:assert 'rendering' not in events and 'candidate' not in events
        assert all('controlled-render-failure' not in p and 'a'*64 not in p for p in provider.prompts)
        measures=summary(store,task)
        assert measures['model_calls']==2 and measures['tool_calls']==1 and measures['repair_events']==0
    finally:
        await runner.pause();store.close()


@pytest.mark.asyncio
async def test_pause_during_boundary_render_preserves_successful_receipt(tmp_path,monkeypatch):
    store=ProjectStore(tmp_path);provider=CreationProvider();runner=TaskRunner(store,provider)
    entered=asyncio.Event();settled=asyncio.Event()
    async def render(**kwargs):
        entered.set()
        try:await asyncio.Event().wait()
        finally:settled.set()
    monkeypatch.setattr(runner.registry.word_renderer,'render',render)
    try:
        task=await runner.start('Create report',artifact='report.docx')
        await asyncio.wait_for(entered.wait(),5)
        await runner.pause()
        assert settled.is_set() and provider.calls==1
        assert store.candidate('report.docx') and not (tmp_path/'report.docx').exists()
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='paused'
        observations=summary(store,task)['observations']
        assert next(x for x in observations if x['kind']=='tool')['status']=='completed'
        assert next(x for x in observations if x['kind']=='render')['status']=='cancelled'
        await runner.start('Continue',artifact='report.docx',task_id=task)
        await runner.job
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
    finally:
        await runner.pause();store.close()
