import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.content_plan import plan
from localoffice.errors import HarnessError
from test_word_progress import task


def setup(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Use First page and Second page. Header Quarterly report.' WHERE id='task'")
    receipt=plan(store,'task',literals=[{'role':'paragraph','quote':'First page'},{'role':'paragraph','quote':'Second page'},{'role':'header','quote':'Quarterly report'}])
    refs=[item['ref'] for item in receipt['literals']]
    request={'path':'report.docx','blocks':[{'kind':'paragraph','text':''},{'kind':'paragraph','text':'','page_break_before':True}],
        'header':{'text':'','alignment':'right'},'literal_refs':{'blocks.0.text':refs[0],'blocks.1.text':refs[1],'header.text':refs[2]}}
    return store,WordPlugin(store),request


def test_creation_resolves_literals_and_replays_without_source_lookup(tmp_path,monkeypatch):
    store,plugin,request=setup(tmp_path)
    try:
        result=plugin.create(**request,task_id='task',idempotency_key='literal-create')
        objects=plugin.read_objects(path='report.docx',revision=result['candidate_revision'],ids=['paragraph:1','paragraph:2'])['objects']
        assert [o['text'] for o in objects]==['First page','Second page']
        saved=json.loads(store.db.execute('SELECT args FROM operations WHERE id=?',(result['receipt_id'],)).fetchone()[0])
        assert saved['submitted_request']['blocks'][0]['text']==''
        assert saved['request']['blocks'][0]['text']=='First page'
        assert 'literal_refs' not in saved['request'] and len(saved['literal_bindings'])==3
        assert not (tmp_path/'report.docx').exists()
        with store.transaction():store.db.execute('DELETE FROM word_content_refs')
        def forbidden(*args,**kwargs):raise AssertionError('Replay cannot rerun worker')
        monkeypatch.setattr(plugin,'_execute',forbidden)
        assert plugin.create(**request,task_id='task',idempotency_key='literal-create')==result
        changed={**request,'literal_refs':{**request['literal_refs'],'blocks.0.text':'ct_'+'0'*24}}
        with pytest.raises(HarnessError,match='different arguments'):plugin.create(**changed,task_id='task',idempotency_key='literal-create')
    finally:store.close()


@pytest.mark.parametrize('change',['role','competing','slot','stale','other_task'])
def test_bad_binding_never_stages_a_document(tmp_path,change):
    store,plugin,request=setup(tmp_path)
    try:
        owner='task'
        if change=='role':request['literal_refs']['blocks.0.text']=request['literal_refs']['header.text']
        elif change=='competing':request['blocks'][0]['text']='Invented paragraph'
        elif change=='slot':request['literal_refs']['styles.0.font']=request['literal_refs'].pop('blocks.0.text')
        elif change=='stale':
            with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed First page' WHERE id='task'")
        else:task(store,'other');owner='other'
        with pytest.raises(HarnessError):plugin.create(**request,task_id=owner,idempotency_key='bad')
        assert store.candidate('report.docx') is None
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize("inline",[False,True])
def test_native_reconciliation_and_undo_with_literal_binding(tmp_path,inline):
    from localoffice.native_history import record_call,operation_key,reconcile_word_call
    store,plugin,request=setup(tmp_path)
    if inline:
        refs=request.pop('literal_refs')
        for i in range(2):request['blocks'][i]['text']={'ref':refs[f'blocks.{i}.text']}
        request['header']['text']={'ref':refs['header.text']}
    call=record_call(store,'task',{'tool':'word.create',**request})
    result=plugin.create(**request,task_id='task',idempotency_key=operation_key(call))
    store.close();store=ProjectStore(tmp_path)
    try:
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='save')
        saved=(tmp_path/'report.docx').read_bytes()
        undo=store.undo(result['receipt_id'],'undo-literal');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        assert not (tmp_path/'report.docx').exists()
        restore=store.undo(undo['id'],'restore-literal');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==saved
    finally:store.close()


@pytest.mark.asyncio
async def test_native_plan_to_creation_uses_exact_literals(tmp_path):
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import validate_history
    store=ProjectStore(tmp_path)
    class Provider:
        native_tool_protocol=True
        context_limit=16384
        calls=0
        refs=None
        def status(self):return {'loaded':True,'constrained_decoding':False}
        async def count_tokens(self,messages,*,tools):validate_history(messages);return 500
        async def stream(self,messages,**kwargs):
            if self.calls==0:action={'tool':'word.load_tools','operations':['plan_content']}
            elif self.calls==1:action={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'First page'},{'role':'paragraph','quote':'Second page'}]}
            elif self.calls==2:
                receipt=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1]);self.refs=[x['ref'] for x in receipt['literals']]
                assert receipt['source']=='objective'
                assert [x['text'] for x in receipt['literals']]==['First page','Second page']
                assert all(not {'source_hash','spans','start','end'} & x.keys() for x in receipt['literals'])
                action={'tool':'word.load_tools','operations':['create']}
            elif self.calls==3:action={'tool':'word.create','path':'report.docx','blocks':[{'kind':'paragraph','text':{'ref':ref}} for ref in self.refs]}
            else:action={'tool':'finish','text':'Ready'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        key=await runner.start('Create report.docx with literal paragraphs First page and Second page.',artifact='report.docx',mode='background');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed',runner.events
        candidate=store.candidate('report.docx')
        result=WordPlugin(store).read_objects(path='report.docx',revision=candidate['candidate_hash'],ids=['paragraph:1','paragraph:2'])
        assert [o['text'] for o in result['objects']]==['First page','Second page']
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(key,)).fetchone()[0]==1
        raw=[json.loads(row[0]) for row in store.db.execute("SELECT content FROM messages WHERE task_id=? AND role='tool'",(key,))]
        planned=next(x for x in raw if x.get('status')=='planned')
        saved=json.loads(store.db.execute('SELECT args FROM operations WHERE task_id=?',(key,)).fetchone()[0])
        assert [x['source_hash'] for x in saved['literal_bindings']]==[planned['source_hash']]*2
        assert [{k:v for k,v in x.items() if k!='slot'} for x in saved['literal_bindings']]==planned['literals']
    finally:await runner.pause();store.close()


def test_literal_footer_and_table_cell_bind_without_changing_layout(tmp_path):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Footer Page . Cell Δ = 42.' WHERE id='task'")
    try:
        literals=plan(store,'task',literals=[{'role':'footer','quote':'Page '},{'role':'table_cell','quote':'Δ = 42'}])['literals']
        request=CreateDocument(path='report.docx',blocks=[{'kind':'table','rows':[['']]}],footer={'text':'','alignment':'center','page_numbers':'current_total','separator':' of '},literal_refs={'footer.text':literals[0]['ref'],'blocks.0.rows.0.0':literals[1]['ref']})
        resolved,evidence=bind_creation(store,'task',request)
        assert resolved.footer.text=='Page ' and resolved.footer.page_numbers=='current_total'
        assert resolved.footer.alignment=='center' and resolved.footer.separator==' of '
        assert resolved.blocks[0].rows==[['Δ = 42']]
        assert request.footer.text=='' and request.blocks[0].rows==[['']]
        assert len(evidence)==2
    finally:store.close()


def test_compact_reference_binds_full_message_literal_after_restart(tmp_path):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    from localoffice.word.evidence import model_receipt
    store=ProjectStore(tmp_path);task(store)
    text='Exact Δ paragraph. '*100
    message=store.message('user',text+' Header Header','task')
    full=plan(store,'task',source_message=message,literals=[{'role':'paragraph','quote':text},{'role':'header','quote':'Header'}])
    compact=model_receipt(full)
    store.close();store=ProjectStore(tmp_path)
    try:
        request=CreateDocument(path='report.docx',blocks=[{'kind':'paragraph','text':''}],header={'text':''},literal_refs={'blocks.0.text':compact['literals'][0]['ref'],'header.text':compact['literals'][1]['ref']})
        resolved,bindings=bind_creation(store,'task',request)
        assert resolved.blocks[0].text==text and resolved.header.text=='Header'
        assert bindings==[{'slot':slot,**entry} for slot,entry in zip(['blocks.0.text','header.text'],full['literals'])]
        with store.transaction():store.db.execute('UPDATE messages SET content=? WHERE id=?',(text+' changed Header Header',message))
        with pytest.raises(HarnessError):bind_creation(store,'task',request)
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def inline_setup(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Exact paragraph. Header Label. Footer Page . Cell Δ42. Legacy paragraph.' WHERE id='task'")
    selections=[('paragraph','Exact paragraph'),('header','Label'),('footer','Page '),('table_cell','Δ42'),('paragraph','Legacy paragraph')]
    literals=plan(store,'task',literals=[{'role':role,'quote':text} for role,text in selections])['literals']
    refs=[{'ref':entry['ref']} for entry in literals]
    payload={'path':'report.docx','blocks':[{'kind':'paragraph','text':refs[0],'style':'Body'},
        {'kind':'paragraph','text':'Drafted prose','page_break_before':True},
        {'kind':'table','rows':[[refs[3],'Plain cell']]},{'kind':'paragraph','text':''}],
        'styles':[{'id':'Body','font':'Calibri','size_points':11}],
        'header':{'text':refs[1],'alignment':'right'},
        'footer':{'text':refs[2],'alignment':'center','page_numbers':'current_total','separator':' of '},
        'literal_refs':{'blocks.3.text':literals[4]['ref']}}
    return store,payload,literals


def test_inline_all_slots_mixed_drafting_legacy_and_layout_are_preserved(tmp_path):
    from localoffice.word.content_plan import bind_creation,has_bindings
    from localoffice.word.contracts import CreateDocument
    store,payload,literals=inline_setup(tmp_path)
    try:
        request=CreateDocument(**payload);before=request.model_dump()
        assert has_bindings(request)
        resolved,bindings=bind_creation(store,'task',request)
        assert request.model_dump()==before
        assert resolved.blocks[0].text=='Exact paragraph'
        assert resolved.blocks[1].text=='Drafted prose' and resolved.blocks[1].page_break_before
        assert resolved.blocks[2].rows==[['Δ42','Plain cell']]
        assert resolved.blocks[3].text=='Legacy paragraph'
        assert resolved.header.text=='Label' and resolved.header.alignment=='right'
        assert resolved.footer.text=='Page ' and resolved.footer.separator==' of '
        assert resolved.footer.page_numbers=='current_total' and resolved.footer.alignment=='center'
        assert resolved.styles==request.styles and resolved.blocks[0].style=='Body'
        assert resolved.literal_refs=={} and not has_bindings(resolved)
        by_ref={entry['ref']:entry for entry in literals}
        assert len(bindings)==5
        for entry in bindings:assert {k:v for k,v in entry.items() if k!='slot'}==by_ref[entry['ref']]
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('kind',['paragraph','header','footer','table_cell'])
def test_has_bindings_detects_each_inline_slot_without_legacy_map(tmp_path,kind):
    from localoffice.word.content_plan import has_bindings
    from localoffice.word.contracts import CreateDocument
    store,payload,literals=inline_setup(tmp_path)
    try:
        reference=next({'ref':x['ref']} for x in literals if x['role']==kind)
        data={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Plain'}]}
        assert not has_bindings(CreateDocument(**data))
        if kind=='paragraph':data['blocks'][0]['text']=reference
        elif kind=='table_cell':data['blocks']=[{'kind':'table','rows':[[reference]]}]
        else:data[kind]={'text':reference}
        assert has_bindings(CreateDocument(**data))
    finally:store.close()


@pytest.mark.parametrize('same_ref',[True,False])
def test_inline_and_legacy_overlap_is_rejected_even_when_identical(tmp_path,same_ref):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    store,payload,literals=inline_setup(tmp_path)
    try:
        payload['literal_refs']['blocks.0.text']=literals[0 if same_ref else 4]['ref']
        request=CreateDocument(**payload);before=request.model_dump()
        with pytest.raises(HarnessError,match='both an inline reference'):bind_creation(store,'task',request)
        assert request.model_dump()==before
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('change',['stale','role','other_task','other_chat','missing'])
def test_inline_bindings_preserve_source_role_and_owner_checks(tmp_path,change):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    from localoffice.chats import create_chat,select_chat
    store,payload,literals=inline_setup(tmp_path)
    owner='task'
    try:
        if change=='stale':
            with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        elif change=='role':payload['blocks'][0]['text']={'ref':literals[1]['ref']}
        elif change=='other_task':task(store,'other');owner='other'
        elif change=='other_chat':select_chat(store,create_chat(store))
        else:payload['blocks'][0]['text']={'ref':'ct_'+'0'*24}
        with pytest.raises(HarnessError):bind_creation(store,owner,CreateDocument(**payload))
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('role',['header','footer','table_cell'])
def test_inline_historical_oversized_literal_fails_resolved_slot_validation(tmp_path,role):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    from localoffice.storage import canonical,digest
    from pydantic import ValidationError
    store=ProjectStore(tmp_path);task(store)
    text='x'*2001
    try:
        with store.transaction():store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(text,'task'))
        revision=digest(text.encode())
        ref='ct_'+digest(canonical(['task','objective',revision,role,0,len(text)]).encode())[:24]
        historical={'ref':ref,'role':role,'text':text,'source':'objective','source_hash':revision,'start':0,'end':len(text)}
        with store.transaction():store.db.execute('INSERT INTO word_content_refs VALUES(?,?,?)',(ref,'task',canonical(historical)))
        data={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Plain'}]}
        if role=='table_cell':data['blocks']=[{'kind':'table','rows':[[{'ref':ref}]]}]
        else:data[role]={'text':{'ref':ref}}
        with pytest.raises(ValidationError):bind_creation(store,'task',CreateDocument(**data))
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def test_inline_bindings_keep_combined_reference_limit(tmp_path):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    store,payload,literals=inline_setup(tmp_path)
    try:
        cell={'ref':literals[3]['ref']}
        request=CreateDocument(path='report.docx',blocks=[{'kind':'table','rows':[[cell]*9 for _ in range(3)]}])
        with pytest.raises(HarnessError,match='24 combined'):bind_creation(store,'task',request)
    finally:store.close()


@pytest.mark.parametrize('with_legacy',[True,False])
def test_inline_creation_ledger_restart_and_exact_replay(tmp_path,monkeypatch,with_legacy):
    from localoffice.word.content_plan import has_bindings
    from localoffice.word.contracts import CreateDocument
    store,payload,literals=inline_setup(tmp_path)
    try:
        if not with_legacy:
            payload.pop('literal_refs')
            payload['blocks'][3]['text']='Requested draft'
        plugin=WordPlugin(store)
        result=plugin.create(**payload,task_id='task',idempotency_key='inline-create')
        saved=json.loads(store.db.execute('SELECT args FROM operations WHERE id=?',(result['receipt_id'],)).fetchone()[0])
        assert saved['submitted_request']['blocks'][0]['text']=={'ref':literals[0]['ref']}
        if with_legacy:assert saved['submitted_request']['literal_refs']==payload['literal_refs']
        else:assert 'literal_refs' not in saved['submitted_request']
        assert saved['request']['blocks'][0]['text']=='Exact paragraph'
        assert saved['request']['footer']['text']=='Page '
        assert not has_bindings(CreateDocument(**saved['request']))
        assert len(saved['literal_bindings'])==(5 if with_legacy else 4)
        store.close();store=ProjectStore(tmp_path);plugin=WordPlugin(store)
        with store.transaction():store.db.execute('DELETE FROM word_content_refs')
        def forbidden(*args,**kwargs):raise AssertionError('Replay must not invoke worker or bind source')
        monkeypatch.setattr(plugin,'_execute',forbidden)
        monkeypatch.setattr('localoffice.word.content_plan.bind_creation',forbidden)
        assert plugin.create(**payload,task_id='task',idempotency_key='inline-create')==result
        changed=json.loads(json.dumps(payload));changed['blocks'][0]['text']={'ref':literals[4]['ref']}
        with pytest.raises(HarnessError,match='different arguments'):plugin.create(**changed,task_id='task',idempotency_key='inline-create')
    finally:store.close()
