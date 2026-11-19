"""Explicit opt-in proposal review; source absence is never a mutation failure."""
import json,io
import pytest
from pypdf import PdfReader
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore
from localoffice.errors import HarnessError
from localoffice.word.content_plan import active_plan
from localoffice.word.creation_repair import pending,get
from test_native_runner import NativeProvider

OBJECTIVE='Create First page and Second page. Header Quarterly report. Footer Page followed by native page numbers. Arial 11 body and 9 bands.'
LITERALS=[{'role':'paragraph','quote':'First page'},{'role':'paragraph','quote':'Second page'},
          {'role':'header','quote':'Quarterly report'},{'role':'footer','quote':'Page '}]
PROPOSAL={'path':'report.docx','blocks':[{'kind':'paragraph','text':'First page'},
          {'kind':'paragraph','text':'Second page','page_break_before':True}],
          'header':{'text':'Quarterly report','alignment':'right'},
          'footer':{'text':'Page of','alignment':'center','page_numbers':'current_total','separator':' of '}}


@pytest.mark.asyncio
@pytest.mark.parametrize('update',['replace','revise'])
async def test_review_replacement_then_explicit_finalization_is_one_creation(tmp_path,update):
    store=ProjectStore(tmp_path)
    class Provider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            task=store.db.execute('SELECT id FROM tasks').fetchone()[0]
            names={t['function']['name'] for t in tools}
            if self.calls==0:action={'tool':'word.plan_content','literals':LITERALS}
            elif self.calls==1:
                assert 'word.prepare_creation' in names and 'word.plan_content' not in names
                assert store.candidate('report.docx') is None
                loader=next(t['function'] for t in tools if t['function']['name']=='word.load_tools')
                assert 'create' not in loader['parameters']['properties']['operations']['items']['enum']
                action={'tool':'word.load_tools','operations':['prepare_creation']}
            elif self.calls==2:
                assert 'word.prepare_creation' in names and 'word.create' not in names
                action={'tool':'word.prepare_creation',**PROPOSAL}
            elif self.calls==3:
                self.old_ref=pending(store,task)['ref']
                assert 'details_in_current_state' in messages[-1]['content']
                assert 'proposed_literals' in messages[0]['content']
                assert 'proposed_design' in messages[0]['content']
                assert any('proposed_design' in row[0] for row in store.db.execute("SELECT content FROM messages WHERE role='tool'"))
                assert pending(store,task)['status']=='prepared'
                assert 'proposed_design' in pending(store,task)
                assert store.candidate('report.docx') is None
                assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
                assert 'word.finalize_creation' in names
                action={'tool':'word.load_tools','operations':['revise_creation' if update=='revise' else 'prepare_creation']}
            elif self.calls==4:
                proposal=json.loads(json.dumps(PROPOSAL))
                proposal['styles']=[{'id':'Band','font':'Arial','size_points':9}]
                proposal['header']['style']='Band';proposal['footer']['style']='Band'
                proposal['footer']['text']='Page '
                action=({'tool':'word.revise_creation','ref':self.old_ref,'changes':[
                    {'op':'set','path':'styles','value':proposal['styles']},
                    {'op':'set','path':'header.style','value':'Band'},
                    {'op':'set','path':'footer.style','value':'Band'},
                    {'op':'set','path':'footer.text','value':'Page '}]} if update=='revise' else {'tool':'word.prepare_creation',**proposal})
            elif self.calls==5:
                state=pending(store,task);assert state['ref']!=self.old_ref
                with pytest.raises(HarnessError):get(store,task,self.old_ref)
                ref=next(x['ref'] for x in active_plan(store,task)['refs'] if x['role']=='footer')
                action={'tool':'word.finalize_creation','ref':state['ref'],'styles':[],
                        'bindings':[{'slot':'footer.text','ref':ref}]}
            else:
                assert self.calls==6 and 'finish' in names
                assert '"current_render"' in messages[0]['content']
                assert '"pages":2' in messages[0]['content']
                action={'tool':'finish','text':'Ready for review.'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Provider(None);runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        task=await runner.start(OBJECTIVE,artifact='report.docx',mode='live');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed',runner.events
        assert provider.calls==7 and store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        previews=[x for x in runner.events if x['type']=='candidate' and x.get('render')];assert len(previews)==1
        pdf=PdfReader(io.BytesIO(store.blob(previews[0]['render']['pdf_hash'])))
        assert len(pdf.pages)==2
        for n,page in enumerate(pdf.pages,1):assert f'Page {n} of 2' in ' '.join(page.extract_text().split())
        candidate=store.candidate('report.docx');assert not (tmp_path/'report.docx').exists()
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        data=(tmp_path/'report.docx').read_bytes()
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        redo=store.undo(undo['id'],'redo');store.apply(redo['id'],expected_candidate=redo['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==data
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_review_mode_is_pinned_across_resume(tmp_path):
    store=ProjectStore(tmp_path)
    class Ask(NativeProvider):
        async def stream(self,messages,**kwargs):
            self.calls+=1
            yield {'type':'token','text':'<tool_call>{"name":"ask_user","arguments":{"text":"Review stop."}}</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Ask(None);runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        task=await runner.start(OBJECTIVE,artifact='report.docx');await runner.job
        runner.creation_review=False
        await runner.run(task,'live')
        assert provider.calls==1
        assert any(x.get('code')=='content_mode_change' for x in runner.events)
    finally:await runner.pause();store.close()


def test_review_requires_native_source_planning(tmp_path):
    store=ProjectStore(tmp_path)
    try:
        with pytest.raises(HarnessError,match='requires native tools and source planning'):
            TaskRunner(store,NativeProvider(None),creation_review=True)
    finally:store.close()
