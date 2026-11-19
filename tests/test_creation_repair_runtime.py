import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.word.plugin import WordPlugin
from localoffice.word.creation_repair import capture
from localoffice.native_history import record_call,operation_key,reconcile_word_call
from localoffice.errors import HarnessError
from test_native_runner import NativeProvider
from test_word_progress import task

REQUEST={'tool':'word.create','path':'report.docx','blocks':[{'kind':'paragraph','text':'Exact body'}],
         'header':{'text':'Exact header','style':'Band'},'footer':{'text':'Page ','style':'Band','page_numbers':'current_total'}}
STYLES=[{'id':'Band','font':'Arial','size_points':9.0}]


@pytest.mark.parametrize("inline",[False,True])
def test_repair_ledger_restart_reconcile_replay_undo(tmp_path,monkeypatch,inline):
    store=ProjectStore(tmp_path);task(store)
    request=json.loads(json.dumps(REQUEST))
    if inline:
        from localoffice.word.content_plan import plan
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective=? WHERE id='task'",('Exact body; Exact header; Page ',))
        refs=plan(store,'task',literals=[{'role':'paragraph','quote':'Exact body'},{'role':'header','quote':'Exact header'},{'role':'footer','quote':'Page '}])['literals']
        request['blocks'][0]['text']={'ref':refs[0]['ref']}
        request['header']['text']={'ref':refs[1]['ref']}
        request['footer']['text']={'ref':refs[2]['ref']}
    hint=capture(store,'task',request)
    action={'tool':'word.repair_creation','ref':hint['ref'],'styles':STYLES}
    call=record_call(store,'task',action)
    result=WordPlugin(store).repair_creation(ref=hint['ref'],styles=STYLES,task_id='task',idempotency_key=operation_key(call))
    saved=json.loads(store.operation(result['receipt_id'])['args'])
    assert saved['repair_origin']['payload']==request
    if inline:
        assert saved['submitted_request']['blocks'][0]['text']==request['blocks'][0]['text']
        assert len(saved['literal_bindings'])==3
    assert saved['request']['blocks'][0]['text']=='Exact body'
    assert saved['request']['header']['text']=='Exact header'
    assert saved['repair_request']['ref']==hint['ref']
    store.close();store=ProjectStore(tmp_path)
    try:
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        plugin=WordPlugin(store)
        monkeypatch.setattr(plugin,'_execute',lambda *a,**kw:pytest.fail('Replay must not execute'))
        assert plugin.repair_creation(ref=hint['ref'],styles=STYLES,task_id='task',idempotency_key=operation_key(call))==result
        with pytest.raises(HarnessError,match='different arguments'):
            plugin.repair_creation(ref=hint['ref'],styles=[{'id':'Band','size_points':10.0}],task_id='task',idempotency_key=operation_key(call))
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='save')
        undo=store.undo(result['receipt_id'],'undo-repair')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        assert not (tmp_path/'report.docx').exists()
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',('task',)).fetchone()[0]==1
    finally:store.close()


@pytest.mark.asyncio
async def test_native_missing_style_repair_routes_compact_tool_then_finishes(tmp_path):
    store=ProjectStore(tmp_path)
    class Provider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            names={t['function']['name'] for t in tools}
            if self.calls==0:
                assert 'word.create' in names
                action=dict(REQUEST)
            elif self.calls==1:
                assert 'word.repair_creation' in names and 'word.create' not in names
                hint=json.loads(store.db.execute("SELECT value FROM meta WHERE key LIKE 'word_creation_repair:%'").fetchone()[0])['state']
                action={'tool':'word.repair_creation','ref':hint['ref'],'styles':STYLES}
            else:action={'tool':'finish','text':'Ready'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action.pop('tool'),'arguments':action})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider(None))
    try:
        key=await runner.start('Create exact body and header with Arial 9 point bands.',artifact='report.docx',mode='background')
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed',runner.events
        assert len([e for e in runner.events if e['type']=='tool_error'])==1
        assert store.candidate('report.docx')
        assert not (tmp_path/'report.docx').exists()
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(key,)).fetchone()[0]==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_explicit_create_loading_replaces_pending_repair_route(tmp_path):
    store=ProjectStore(tmp_path)
    class Provider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            names={t['function']['name'] for t in tools}
            if self.calls==0:action=dict(REQUEST)
            elif self.calls==1:
                assert 'word.repair_creation' in names
                action={'tool':'word.load_tools','operations':['create']}
            elif self.calls==2:
                assert 'word.create' in names and 'word.repair_creation' not in names
                action={**REQUEST,'styles':STYLES}
            else:action={'tool':'finish','text':'Ready'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action.pop('tool'),'arguments':action})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider(None))
    try:
        key=await runner.start('Create exact body and header.',artifact='report.docx',mode='background')
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed',runner.events
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(key,)).fetchone()[0]==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_explicit_source_repair_stages_correct_fields_once(tmp_path):
    """Scripted registered-tool loop, not real-model qualification."""
    import io
    from pypdf import PdfReader
    from localoffice.word.content_plan import active_plan
    store=ProjectStore(tmp_path)
    objective='Create two pages: First page and Second page. Header Quarterly report. Footer prefix Page followed by native current and total page fields.'
    creation={'tool':'word.create','path':'report.docx',
              'blocks':[{'kind':'paragraph','text':'First page'},{'kind':'paragraph','text':'Second page','page_break_before':True}],
              'header':{'text':'Quarterly report','style':'Band','alignment':'right'},
              'footer':{'text':'Page of','style':'Band','alignment':'center','page_numbers':'current_total','separator':' of '}}
    class Provider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            if self.calls==0:
                action={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'First page'},
                    {'role':'paragraph','quote':'Second page'},{'role':'header','quote':'Quarterly report'},
                    {'role':'footer','quote':'Page '}]}
            elif self.calls==1:action={'tool':'word.load_tools','operations':['create']}
            elif self.calls==2:action=creation
            elif self.calls==3:
                action={'tool':'word.load_tools','operations':['revise_creation']}
            elif self.calls==4:
                from localoffice.word.creation_repair import pending
                task_id=store.db.execute('SELECT id FROM tasks').fetchone()[0]
                state=pending(store,task_id)
                assert {'slot':'footer.text','text':'Page of'} in state['proposed_literals']['items']
                action={'tool':'word.revise_creation','ref':state['ref'],'changes':[
                    {'op':'set','path':'footer.text','value':'Page '}]}
            elif self.calls==5:
                from localoffice.word.creation_repair import pending
                task_id=store.db.execute('SELECT id FROM tasks').fetchone()[0]
                state=pending(store,task_id)
                ref=next(item['ref'] for item in active_plan(store,task_id)['refs'] if item['role']=='footer')
                action={'tool':'word.finalize_creation','ref':state['ref'],'styles':STYLES,
                        'bindings':[{'slot':'footer.text','ref':ref}]}
            else:
                assert self.calls==6
                assert 'finish' in {t['function']['name'] for t in tools}
                action={'tool':'finish','text':'Staged for review.'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],
                'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Provider(None);runner=TaskRunner(store,provider,source_planning=True)
    try:
        key=await runner.start(objective,artifact='report.docx',mode='live');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed',runner.events
        assert provider.calls==7
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        candidate=store.candidate('report.docx');assert candidate and not (tmp_path/'report.docx').exists()
        preview=next(e for e in runner.events if e['type']=='candidate' and e.get('render'))
        pdf=PdfReader(io.BytesIO(store.blob(preview['render']['pdf_hash'])))
        assert len(pdf.pages)==2
        for i,page in enumerate(pdf.pages,1):
            text=' '.join(page.extract_text().split())
            assert f'Page {i} of 2' in text and 'Quarterly report' in text and 'Page of' not in text
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        data=(tmp_path/'report.docx').read_bytes()
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        restore=store.undo(undo['id'],'restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==data
    finally:await runner.pause();store.close()
