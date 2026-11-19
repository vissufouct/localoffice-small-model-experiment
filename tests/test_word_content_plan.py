import pytest
from localoffice.storage import ProjectStore
from localoffice.word.content_plan import plan,resolve
from localoffice.errors import HarnessError
from localoffice.chats import create_chat,select_chat
from test_word_progress import task


def fixture(tmp_path,objective='Use First page and Second page. Keep Δ = 2 and 😀 unchanged.'):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(objective,'task'))
    return store


def test_exact_literals_are_stable_and_survive_restart(tmp_path):
    store=fixture(tmp_path)
    request={'literals':[{'role':'paragraph','quote':'First page'},{'role':'paragraph','quote':'Δ = 2 and 😀'}]}
    result=plan(store,'task',**request)
    assert plan(store,'task',**request)==result
    assert result['document_changed'] is False
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    store.close();store=ProjectStore(tmp_path)
    try:
        assert resolve(store,'task',result['literals'][1]['ref'],role='paragraph')=='Δ = 2 and 😀'
        with pytest.raises(HarnessError):resolve(store,'task',result['literals'][0]['ref'],role='footer')
    finally:store.close()


def test_paraphrase_is_rejected_atomically_and_ambiguity_is_explicit(tmp_path):
    store=fixture(tmp_path,'Use Page twice: Page.')
    try:
        with pytest.raises(HarnessError,match='exact passage'):
            plan(store,'task',literals=[{'role':'paragraph','quote':'Page','occurrence':0},{'role':'header','quote':'Invented report prose'}])
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        result=plan(store,'task',literals=[{'role':'paragraph','quote':'Page','occurrence':1}])
        assert result['literals'][0]['start']==16
    finally:store.close()


def test_refs_reject_changed_source_wrong_task_chat_and_forget(tmp_path):
    from localoffice.memory import forget_project_memory
    store=fixture(tmp_path);task(store,'other')
    try:
        ref=plan(store,'task',literals=[{'role':'header','quote':'First page'}])['literals'][0]['ref']
        with pytest.raises(HarnessError):resolve(store,'other',ref,role='header')
        original_chat=store.db.execute("SELECT value FROM meta WHERE key='active_chat'").fetchone()[0]
        select_chat(store,create_chat(store))
        with pytest.raises(HarnessError):resolve(store,'task',ref,role='header')
        select_chat(store,original_chat)
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed First page' WHERE id='task'")
        with pytest.raises(HarnessError):resolve(store,'task',ref,role='header')
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


def test_source_message_must_be_user_in_same_task(tmp_path):
    store=fixture(tmp_path);task(store,'other')
    try:
        user=store.message('user','Use revised heading','task')
        assistant=store.message('assistant','Use revised heading','task')
        other=store.message('user','Use revised heading','other')
        request={'literals':[{'role':'header','quote':'revised heading'}]}
        result=plan(store,'task',source_message=user,**request)
        assert resolve(store,'task',result['literals'][0]['ref'],role='header')=='revised heading'
        for source_message in (assistant,other):
            with pytest.raises(HarnessError):plan(store,'task',source_message=source_message,**request)
    finally:store.close()


@pytest.mark.asyncio
async def test_registered_tool_is_lazily_exposed_and_does_not_write_document(tmp_path):
    from localoffice.plugins import first_party_registry
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    from localoffice.native_tools import action_tools
    store=fixture(tmp_path)
    try:
        registry=first_party_registry(store)
        result=await registry.call('word.plan_content',task_id='task',literals=[{'role':'paragraph','quote':'First page'}])
        assert result['status']=='planned'
        def names(operations):return {t['function']['name'] for t in action_tools(project_schema(WORD_ACTION.json_schema(),operations))}
        assert 'word.plan_content' not in names([])
        assert 'word.plan_content' in names(['plan_content'])
        assert 'word.stage_batch' not in names(['plan_content'])
        assert not (tmp_path/'report.docx').exists()
    finally:store.close()


def test_content_reference_graph_is_revision_bound(tmp_path):
    import json
    store=fixture(tmp_path)
    try:
        receipt=plan(store,'task',literals=[{'role':'paragraph','quote':'First page'}])
        item=receipt['literals'][0]
        row=store.db.execute('SELECT body FROM nodes WHERE id=?',('content:'+item['ref'],)).fetchone()
        assert json.loads(row[0])==item
        assert item['source_hash']==receipt['source_hash']
        assert resolve(store,'task',item['ref'],role='paragraph')=='First page'
    finally:store.close()


def test_all_selection_errors_reported_without_partial_writes(tmp_path):
    store=fixture(tmp_path,'Keep label and label with footer Page and fields.')
    try:
        with pytest.raises(HarnessError) as error:
            plan(store,'task',literals=[
                {'role':'header','quote':'Keep'},
                {'role':'paragraph','quote':'label'},
                {'role':'footer','quote':'Page of'},
                {'role':'paragraph','quote':'fields','occurrence':3},
            ])
        assert error.value.code=='content_quote'
        message=str(error.value)
        assert 'literals.1.occurrence' not in message
        assert 'literals.2.quote' in message and 'not an exact passage' in message
        assert 'literals.3.occurrence' in message and '0 to 0' in message
        assert 'literals.0' not in message
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        assert store.db.execute("SELECT count(*) FROM nodes WHERE kind='literal_reference'").fetchone()[0]==0
        receipt=plan(store,'task',literals=[
            {'role':'header','quote':'Keep'},
            {'role':'paragraph','quote':'label','occurrence':1},
            {'role':'footer','quote':'Page'},
            {'role':'paragraph','quote':'fields','occurrence':0},
        ])
        assert len(receipt['literals'])==4
    finally:store.close()


def test_reference_summary_is_bounded_and_omits_stale_sources(tmp_path):
    from localoffice.word.content_plan import summary
    from localoffice.storage import canonical
    objective=' '.join(f'Label {i:02d}' for i in range(24))
    store=fixture(tmp_path,objective)
    try:
        plan(store,'task',literals=[{'role':'paragraph','quote':f'Label {i:02d}'} for i in range(24)])
        result=summary(store,'task',max_chars=450)
        assert 0<len(result['references'])<24 and result['truncated']
        assert len(canonical(result['references']))<=450
        for item in result['references']:
            assert resolve(store,'task',item['ref'],role=item['role'])==item['preview']
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        assert summary(store,'task')['references']==[]
    finally:store.close()


def test_quote_hint_is_exact_bounded_evidence_not_a_saved_substitution(tmp_path):
    from localoffice.word.content_plan import quote_hint
    store=fixture(tmp_path,'Use Summary followed by a computed date and total.')
    try:
        with pytest.raises(HarnessError) as error:
            plan(store,'task',literals=[{'role':'header','quote':'Summary total'}])
        assert '"exact_prefix":"Summary "' in str(error.value)
        assert 'computed date' in str(error.value)
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        assert quote_hint('Completely unrelated','Summary total')==''
        assert quote_hint('Δ = 3 now','Δ = 3 later').startswith(' Source evidence')
        assert len(quote_hint('Z'*60000+' Summary follows.','Summary '+('bad '*1500)))<400
    finally:store.close()


def test_single_prefix_candidate_requires_explicit_resubmission(tmp_path):
    store=fixture(tmp_path,'Body Overview. Header Summary followed by fields and total.')
    try:
        with pytest.raises(HarnessError) as error:
            plan(store,'task',literals=[{'role':'paragraph','quote':'Overview'},{'role':'header','quote':'Summary total'}])
        recovery=error.value.recovery
        assert recovery['candidate_only'] is True
        call=recovery['candidate_call']
        assert call['literals'][0]=={'role':'paragraph','quote':'Overview'}
        assert call['literals'][1]=={'role':'header','quote':'Summary ','occurrence':0}
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        result=plan(store,'task',**call)
        assert result['literals'][1]['text']=='Summary '
        assert result['document_changed'] is False
    finally:store.close()


@pytest.mark.parametrize('objective,literals',[
    ('Summary and Summary after fields.',[{'role':'header','quote':'Summary total'}]),
    ('Summary followed by fields.',[{'role':'header','quote':'Summary total'},{'role':'paragraph','quote':'Missing'}]),
    ('Summary followed by fields.',[{'role':'header','quote':'Summary '},{'role':'header','quote':'Summary total'}]),
])
def test_prefix_recovery_abstains_for_ambiguous_multiple_or_duplicate_selections(tmp_path,objective,literals):
    store=fixture(tmp_path,objective)
    try:
        with pytest.raises(HarnessError) as error:plan(store,'task',literals=literals)
        assert error.value.recovery['kind']=='selection_review'
        assert 'candidate_call' not in error.value.recovery
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


def test_repeated_identical_text_retains_all_locations_without_guessing(tmp_path):
    import json
    store=fixture(tmp_path,'Use Title and Title.')
    try:
        receipt=plan(store,'task',literals=[{'role':'paragraph','quote':'Title'}])
        entry=receipt['literals'][0]
        assert entry['spans']==[[4,9],[14,19]]
        assert 'start' not in entry and 'end' not in entry
        assert resolve(store,'task',entry['ref'],role='paragraph')=='Title'
        assert plan(store,'task',literals=[{'role':'paragraph','quote':'Title'}])==receipt
        store.close();store=ProjectStore(tmp_path)
        assert resolve(store,'task',entry['ref'],role='paragraph')=='Title'
        entry['spans']=entry['spans'][:1]
        with store.transaction():store.db.execute('UPDATE word_content_refs SET body=? WHERE ref=?',(json.dumps(entry),entry['ref']))
        with pytest.raises(HarnessError):resolve(store,'task',entry['ref'],role='paragraph')
    finally:store.close()


def test_many_repeated_matches_require_bounded_explicit_location(tmp_path):
    store=fixture(tmp_path,' '.join(['Title']*33))
    try:
        with pytest.raises(HarnessError,match='more than 32'):plan(store,'task',literals=[{'role':'paragraph','quote':'Title'}])
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        entry=plan(store,'task',literals=[{'role':'paragraph','quote':'Title','occurrence':32}])['literals'][0]
        assert entry['start']==192 and 'spans' not in entry
    finally:store.close()


def test_model_plan_receipt_is_compact_lossless_at_binding_and_non_mutating(tmp_path):
    import copy,json
    from localoffice.word.evidence import model_receipt
    from localoffice.storage import canonical
    long='😀 exact literal '*400
    store=fixture(tmp_path,'Title Title '+long)
    try:
        full=plan(store,'task',literals=[{'role':'header','quote':'Title'},{'role':'paragraph','quote':long}])
        before=copy.deepcopy(full)
        compact=model_receipt(full)
        assert full==before and model_receipt(compact)==compact
        assert compact['source_hash']==full['source_hash']
        assert compact['literals'][0]=={'ref':full['literals'][0]['ref'],'role':'header','text':'Title','matches':2}
        preview=compact['literals'][1]
        assert preview['preview']==long[:80] and preview['text_chars']==len(long)
        assert preview['preview_truncated'] and 'text' not in preview
        assert len(canonical(compact).encode())<1200
        for item in full['literals']:
            assert json.loads(store.db.execute('SELECT body FROM word_content_refs WHERE ref=?',(item['ref'],)).fetchone()[0])==item
            assert json.loads(store.db.execute('SELECT body FROM nodes WHERE id=?',('content:'+item['ref'],)).fetchone()[0])==item
        store.close();store=ProjectStore(tmp_path)
        assert resolve(store,'task',preview['ref'],role='paragraph')==long
    finally:store.close()


def test_maximum_plan_model_receipt_has_fixed_byte_budget(tmp_path):
    from localoffice.word.evidence import model_receipt
    from localoffice.storage import canonical
    # JSON control-character escaping is larger than four-byte Unicode.
    quotes=[str(i)+('\x01'*81) for i in range(24)]
    store=fixture(tmp_path,'|'.join(quotes))
    try:
        full=plan(store,'task',literals=[{'role':'paragraph','quote':quote} for quote in quotes])
        compact=model_receipt(full)
        assert len(compact['literals'])==24
        assert len(canonical(compact).encode())<=16384
        assert [x['ref'] for x in compact['literals']]==[x['ref'] for x in full['literals']]
        assert all(x['preview_truncated'] for x in compact['literals'])
    finally:store.close()


@pytest.mark.parametrize('change',[
    {'ref':'ct_'+'0'*24}, {'text':''}, {'source':'message:-1'},
    {'source':None}, {'start':-1}, {'end':1000}, {'start':False},
    {'spans':[[0,5],[0,5]]},
])
def test_corrupt_literal_references_fail_closed(tmp_path,change):
    import json
    store=fixture(tmp_path,'Title')
    try:
        entry=plan(store,'task',literals=[{'role':'header','quote':'Title'}])['literals'][0]
        ref=entry['ref'];entry.update(change)
        with store.transaction():store.db.execute('UPDATE word_content_refs SET body=? WHERE ref=?',(json.dumps(entry),ref))
        with pytest.raises(HarnessError):resolve(store,'task',ref,role='header')
    finally:store.close()


def test_all_match_limit_includes_overlaps_and_detects_tampering(tmp_path):
    import json
    store=fixture(tmp_path,'a'*33)
    try:
        entry=plan(store,'task',literals=[{'role':'paragraph','quote':'aa'}])['literals'][0]
        assert len(entry['spans'])==32
        assert resolve(store,'task',entry['ref'],role='paragraph')=='aa'
        entry['spans'][0][0]=False
        with store.transaction():store.db.execute('UPDATE word_content_refs SET body=? WHERE ref=?',(json.dumps(entry),entry['ref']))
        with pytest.raises(HarnessError):resolve(store,'task',entry['ref'],role='paragraph')
    finally:store.close()


def test_recovery_does_not_retarget_explicit_occurrence_or_exceed_budget(tmp_path):
    from localoffice.storage import canonical
    store=fixture(tmp_path,'Summary followed by fields. '+('x'*2500))
    try:
        for literals in ([{'role':'header','quote':'Summary total','occurrence':1}],
                         [{'role':'header','quote':'Summary total'},{'role':'paragraph','quote':'x'*2500}]):
            with pytest.raises(HarnessError) as error:plan(store,'task',literals=literals)
            assert error.value.recovery['kind']=='selection_review'
            assert 'candidate_call' not in error.value.recovery
            assert len(canonical(error.value.recovery).encode())<=1024
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('role',['header','footer','table_cell'])
def test_destination_literal_limit_rejects_before_any_write(tmp_path,role):
    from pydantic import ValidationError
    store=fixture(tmp_path,'Keep '+('Δ'*2001))
    try:
        with pytest.raises(ValidationError,match='2000 characters'):
            plan(store,'task',literals=[{'role':'paragraph','quote':'Keep'},{'role':role,'quote':'Δ'*2001}])
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        assert store.db.execute("SELECT count(*) FROM nodes WHERE kind='literal_reference'").fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('role,limit',[('paragraph',8000),('header',2000),('footer',2000),('table_cell',2000)])
def test_destination_literal_boundary_binds_without_truncation(tmp_path,role,limit):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    text='Δ'*limit
    store=fixture(tmp_path,text)
    try:
        entry=plan(store,'task',literals=[{'role':role,'quote':text,'occurrence':0}])['literals'][0]
        data={'path':'report.docx','blocks':[{'kind':'paragraph','text':''}]}
        slot='blocks.0.text'
        if role in ('header','footer'):
            data[role]={'text':''};slot=role+'.text'
        elif role=='table_cell':
            data['blocks']=[{'kind':'table','rows':[['']]}];slot='blocks.0.rows.0.0'
        resolved,bindings=bind_creation(store,'task',CreateDocument(**data,literal_refs={slot:entry['ref']}))
        if role in ('header','footer'):actual=getattr(resolved,role).text
        elif role=='table_cell':actual=resolved.blocks[0].rows[0][0]
        else:actual=resolved.blocks[0].text
        assert actual==text and bindings[0]['text']==text
    finally:store.close()


def test_summary_scans_past_stale_and_malformed_rows_after_restart(tmp_path):
    import json
    from localoffice.word.content_plan import summary
    store=fixture(tmp_path,'Keep')
    try:
        kept=plan(store,'task',literals=[{'role':'paragraph','quote':'Keep'}])['literals'][0]
        # Each newer message becomes stale independently of the old objective.
        for i in range(26):
            message=store.message('user',f'New label {i}','task')
            plan(store,'task',source_message=message,plan_mode='replace',literals=[{'role':'header','quote':f'New label {i}'}])
            with store.transaction():store.db.execute('UPDATE messages SET content=? WHERE id=?',('Changed',message))
        malformed=['{','null','[]','42',json.dumps({'ref':'wrong','role':'paragraph'})]
        with store.transaction():
            for i,body in enumerate(malformed):
                store.db.execute('INSERT INTO word_content_refs VALUES(?,?,?)',(f'broken_{i}','task',body))
        store.close();store=ProjectStore(tmp_path)
        result=summary(store,'task')
        assert result=={'references':[{'ref':kept['ref'],'role':'paragraph','preview':'Keep','truncated':False}],'truncated':False}
        assert summary(store,'task',max_chars=1)=={'references':[],'truncated':True}
    finally:store.close()


def test_summary_marks_reference_and_scan_limits(tmp_path):
    from localoffice.word.content_plan import summary
    store=fixture(tmp_path,' '.join(f'Label-{i:02d}' for i in range(25)))
    try:
        for i in range(25):plan(store,'task',literals=[{'role':'paragraph','quote':f'Label-{i:02d}'}])
        result=summary(store,'task',max_chars=10000)
        assert len(result['references'])==24 and result['truncated']
        # Corrupt overflow is bounded and cannot claim complete navigation.
        with store.transaction():
            for i in range(513):
                store.db.execute('INSERT INTO word_content_refs VALUES(?,?,?)',(f'broken_{i}','task','{'))
        assert summary(store,'task')=={'references':[],'truncated':True}
    finally:store.close()


@pytest.mark.parametrize('slot',['first_header','first_footer','even_header','even_footer'])
@pytest.mark.parametrize('inline',[True,False])
def test_variant_source_binding_is_exact_and_role_scoped(tmp_path,slot,inline):
    from localoffice.word.content_plan import bind_creation
    from localoffice.word.contracts import CreateDocument
    store=fixture(tmp_path,'Keep Exact variant label unchanged.')
    role=slot.rsplit('_',1)[-1]
    try:
        ref=plan(store,'task',literals=[{'role':role,'quote':'Exact variant label'}])['literals'][0]['ref']
        data={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}],
              slot:{'text':{'ref':ref} if inline else ''}}
        if not inline:data['literal_refs']={slot+'.text':ref}
        request=CreateDocument(**data)
        before=request.model_dump()
        resolved,bindings=bind_creation(store,'task',request)
        assert getattr(resolved,slot).text=='Exact variant label'
        assert request.model_dump()==before
        assert bindings[0]['slot']==slot+'.text'
        wrong=plan(store,'task',literals=[{'role':'footer' if role=='header' else 'header','quote':'Exact variant label'}])['literals'][0]['ref']
        data[slot]['text']={'ref':wrong};data.pop('literal_refs',None)
        with pytest.raises(HarnessError):bind_creation(store,'task',CreateDocument(**data))
    finally:store.close()
