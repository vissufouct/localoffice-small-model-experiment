import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.native_tools import validate_history
from localoffice.native_history import exchanges
from test_word_tables import saved_table


class NativeProvider:
    native_tool_protocol=True
    context_limit=16384
    def __init__(self,revision):self.revision=revision;self.calls=0;self.counted=None
    def status(self):return {'loaded':True,'constrained_decoding':False}
    async def count_tokens(self,messages,*,tools):
        validate_history(messages)
        self.counted=(messages,tools)
        return 500
    async def stream(self,messages,*,tools,schema,max_tokens):
        assert schema is None
        assert self.counted==(messages,tools)
        validate_history(messages)
        if self.calls==0:action={'tool':'word.load_tools','operations':['format_text']}
        elif self.calls==1:action={'tool':'word.read_objects','path':'report.docx','revision':self.revision,'ids':['paragraph:2']}
        elif self.calls==2:
            assert messages[-1]['role']=='tool'
            obj=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1])['objects'][0]
            action={'tool':'word.format_text','path':'report.docx','revision':self.revision,'target':obj['id'],'expected_hash':obj['hash'],'start':0,'end':3,'properties':{'bold':True}}
        else:action={'tool':'finish','text':'Ready'}
        self.calls+=1
        yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action.pop('tool'),'arguments':action})+'</tool_call>'}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.asyncio
@pytest.mark.parametrize('constrained',[False,True])
async def test_source_planning_starts_with_plan_tools_and_cannot_change_on_resume(tmp_path,constrained):
    from localoffice.storage import ProjectStore
    store=ProjectStore(tmp_path)
    class Ask(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            names={tool['function']['name'] for tool in tools}
            assert 'word.plan_content' in names
            assert 'word.create' not in names
            assert 'word.load_tools' in names
            assert 'literal_content' in messages[0]['content']
            self.calls+=1
            yield {'type':'token','text':'<tool_call>{"name":"ask_user","arguments":{"text":"Which title?"}}</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Ask(None);provider.native_constrained=constrained
    runner=TaskRunner(store,provider,source_planning=True)
    try:
        key=await runner.start('Create report.docx; ask me for the title.',artifact='report.docx',mode='background')
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused',runner.events
        from localoffice.metrics import summary
        inference=next(item for item in summary(store,key)['observations'] if item['kind']=='inference')
        assert inference['schema_transport'].endswith('experimental constrained generation' if constrained else 'experimental unconstrained generation')
        assert store.db.execute('SELECT value FROM meta WHERE key=?',('task_content_mode:'+key,)).fetchone()[0]=='source_planning'
        await runner.pause();store.close()
        store=ProjectStore(tmp_path)
        runner=TaskRunner(store,provider,source_planning=False)
        await runner.run(key,'background')
        assert any(event.get('code')=='content_mode_change' for event in runner.events)
        assert provider.calls==1
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_word_loop_load_read_edit_finish(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    provider=NativeProvider(revision);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Bold the first three letters in the introduction.',artifact='report.docx',mode='background')
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed',runner.events
        assert provider.calls==4
        pairs=exchanges(store,task)
        assert len(pairs)==4
        assert len({pair[0]['tool_calls'][0]['id'] for pair in pairs})==4
        operation=store.db.execute('SELECT * FROM operations WHERE task_id=?',(task,)).fetchone()
        assert operation['idempotency_key']=='native:'+pairs[2][0]['tool_calls'][0]['id']
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_invalid_calls_stop_without_editing(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    class Invalid(NativeProvider):
        async def stream(self,messages,**kwargs):
            self.calls+=1
            yield {'type':'token','text':'<tool_call>{"name":"invented","arguments":{}}</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Invalid(revision);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Bold the introduction.',artifact='report.docx',mode='background');await runner.job
        assert provider.calls==2
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='failed'
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==0
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_clarification_closes_call_and_pins_protocol(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    class Ask(NativeProvider):
        async def stream(self,messages,**kwargs):
            self.calls+=1
            yield {'type':'token','text':'<tool_call>{"name":"ask_user","arguments":{"text":"Which font?"}}</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Ask(revision);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Change font; ask me first.',artifact='report.docx',mode='background');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='paused'
        assert len(exchanges(store,task))==1
        assert store.db.execute('SELECT value FROM meta WHERE key=?',('task_protocol:'+task,)).fetchone()[0]=='native_qwen_experimental'
        runner.native_tool_protocol=False
        await runner.run(task,'background')
        assert any(event.get('code')=='protocol_change' for event in runner.events)
        assert provider.calls==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_failed_mutation_is_not_cleared_by_read(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    class Broken(NativeProvider):
        async def stream(self,messages,**kwargs):
            actions=[{'tool':'word.load_tools','operations':['create_character_style']},
                {'tool':'word.stage_batch','path':'report.docx','revision':revision,'operations':[{'op':'create_character_style','style_id':'Callout','name':'Callout','inherit_base':False}]},
                {'tool':'word.list_objects','path':'report.docx','revision':revision,'kind':'style'}]
            action=actions[1] if self.calls>=3 else actions[self.calls]
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Broken(revision);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Create independent character style Callout.',artifact='report.docx',mode='background');await runner.job
        assert provider.calls==4
        errors=[e for e in runner.events if e.get('type')=='tool_error']
        assert 'target' in errors[0]['message'] and 'expected_hash' in errors[0]['message']
        assert any(e.get('code')=='retry_limit' for e in runner.events)
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_creation_repairs_with_compact_rejection_and_restores_read_tools(tmp_path):
    from localoffice.storage import ProjectStore
    store=ProjectStore(tmp_path)
    class Creator(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            names={t['function']['name'] for t in tools}
            if self.calls==0:
                assert 'word.create' in names and 'word.read_objects' not in names
                from localoffice.word.guidance import GUIDES
                creation=next(t['function'] for t in tools if t['function']['name']=='word.create')
                assert creation['description']==GUIDES['create']
                assert GUIDES['create'] not in messages[0]['content']
                action={'tool':'word.create','path':'new.docx','blocks':[{'kind':'page_break'}]}
            elif self.calls==1:
                assert any('Rejected native proposal' in m.get('content','') for m in messages)
                assert not any('"kind": "page_break"' in m.get('content','') for m in messages)
                action={'tool':'word.create','path':'new.docx','blocks':[{'kind':'paragraph','text':'Exact supplied text'}]}
            else:
                assert 'word.read_objects' in names
                action={'tool':'finish','text':'Ready'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Creator(None);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Create new.docx with exactly Exact supplied text.',artifact='new.docx',mode='background');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed',runner.events
        assert provider.calls==3
        assert any('"kind": "page_break"' in row[0] for row in store.db.execute("SELECT content FROM messages WHERE role='assistant'"))
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_resuming_with_different_sampling_is_explicitly_refused(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    class Ask(NativeProvider):
        sampling={'temperature':.2}
        async def stream(self,messages,**kwargs):
            self.calls+=1
            yield {'type':'token','text':'<tool_call>{"name":"ask_user","arguments":{"text":"Which font?"}}</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Ask(revision);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Ask me which font to use.',artifact='report.docx',mode='background');await runner.job
        provider.sampling={'temperature':.7}
        await runner.run(task,'background')
        assert provider.calls==1
        assert any(e.get('code')=='sampling_change' for e in runner.events)
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_native_content_recovery_requires_an_explicit_selection_replacement(tmp_path):
    from localoffice.storage import ProjectStore
    store=ProjectStore(tmp_path)
    class Repair(NativeProvider):
        async def stream(self,messages,**kwargs):
            if self.calls==0:
                action={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Overview'},{'role':'header','quote':'Summary total'}]}
            elif self.calls==1:
                error=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1])
                hint=error['recovery']['plan_repair']
                assert hint['status']=='pending'
                assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
                action={'tool':'word.repair_content_plan','ref':hint['ref'],
                        'changes':[{'op':'replace','index':1,
                                    'selection':{'role':'header','quote':'Summary '}}]}
            else:
                assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==2
                action={'tool':'ask_user','text':'Review the planned content.'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Repair(None);runner=TaskRunner(store,provider,source_planning=True)
    try:
        key=await runner.start('Create report.docx with Overview. Header Summary followed by fields and total.',artifact='report.docx');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused',runner.events
        assert provider.calls==3
        assert not store.candidate('report.docx')
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_new_target_planning_hides_reads_until_source_inspect_loaded(tmp_path):
    from localoffice.storage import ProjectStore
    store=ProjectStore(tmp_path)
    class Inspect(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            names={t['function']['name'] for t in tools}
            reads={'word.read_objects','word.list_objects','word.find_text'}
            if self.calls==0:
                assert 'word.plan_content' in names and not names&reads
                assert 'word.load_tools' in names
                assert not names&{'read_file','read_pdf','finish'}
                assert names=={'word.plan_content','word.load_tools','ask_user'}
                action={'tool':'word.load_tools','operations':['inspect']}
            else:
                assert reads|{'read_file','read_pdf'}<=names
                assert 'finish' not in names
                action={'tool':'ask_user','text':'Which source document should be inspected?'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Inspect(None);runner=TaskRunner(store,provider,source_planning=True)
    try:
        key=await runner.start('Create new.docx after I select an existing source.',artifact='new.docx');await runner.job
        assert provider.calls==2
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='paused',runner.events
        assert not store.candidate('new.docx')
    finally:await runner.pause();store.close()
