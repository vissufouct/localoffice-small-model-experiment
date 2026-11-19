"""Explicit retained-proposal binding repair; no worker/model/render calls."""
import copy
import json
import pytest
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.errors import HarnessError
from localoffice.word import creation_repair as repair
from localoffice.word.content_plan import plan,bind_creation,ACTIVE_PLAN_PREFIX
from test_word_progress import task


@pytest.fixture
def context(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective=? WHERE id='task'",('Body source | Header source | Footer source | Cell source | Other source',))
    refs={}
    for role,text in [('paragraph','Body source'),('header','Header source'),('footer','Footer source'),('table_cell','Cell source')]:
        refs[role]=plan(store,'task',literals=[{'role':role,'quote':text}])['literals'][0]['ref']
    payload={'path':'report.docx','styles':[{'id':'Existing','font':'Arial'}],
        'blocks':[{'kind':'paragraph','text':'Original body','style':'Existing'},
                  {'kind':'table','rows':[['Original cell','Unchanged cell']]}],
        'page':{'margin_twips':1000}}
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        payload[name]={'text':'Original '+name,'style':'Band','alignment':'center',
                       'page_numbers':'current_total','separator':' of '}
    yield store,payload,refs
    store.close()


def saved(store):
    return store.db.execute('SELECT value FROM meta WHERE key=?',(repair.PREFIX+'task',)).fetchone()[0]


@pytest.mark.parametrize('slot,role',[
    ('blocks.0.text','paragraph'),('blocks.1.rows.0.0','table_cell'),
    *[(name+'.text',name.rsplit('_',1)[-1]) for name in ('header','footer','first_header','first_footer','even_header','even_footer')]])
def test_identity_bindings_change_only_selected_slot_and_keep_origin(context,slot,role):
    store,payload,refs=context
    parent,key,_=repair._binding_slot(payload,slot);parent[key]=''
    original=copy.deepcopy(payload)
    receipt=repair.capture(store,'task',payload)
    before=repair.get(store,'task')
    styles=[{'id':'Band','size_points':9}]
    plain=repair.resolve(store,'task',receipt['ref'],styles)
    selected={slot:refs[role]}
    request=repair.resolve(store,'task',receipt['ref'],styles,source_bindings=selected)
    expected=plain.model_dump()
    parent,key,_=repair._binding_slot(expected,slot);parent[key]={'ref':refs[role]}
    assert request.model_dump()==expected
    resolved,bindings=bind_creation(store,'task',request)
    parent,key,_=repair._binding_slot(resolved.model_dump(),slot)
    assert parent[key]==role.replace('paragraph','Body').replace('table_cell','Cell').replace('header','Header').replace('footer','Footer')+' source'
    state=repair.get(store,'task')
    assert state['payload']==original==payload and state['payload_hash']==before['payload_hash']
    assert state['literal_bindings']==before['literal_bindings']
    assert state['resolved_source_bindings']==[b for b in bindings if b['slot']==slot]
    assert state['resolved_source_plan']['generation']>=1
    assert state['resolved_hash']==digest(canonical(request.model_dump()).encode())
    assert selected=={slot:refs[role]} and styles==[{'id':'Band','size_points':9}]
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    # A subsequent style-only resolution starts again from the original payload.
    assert repair.resolve(store,'task',receipt['ref'],styles).model_dump()==plain.model_dump()
    assert repair.get(store,'task')['resolved_source_bindings']==[]


@pytest.mark.parametrize('inline',[False,True])
def test_selected_existing_binding_replaced_without_removing_other_bindings(context,inline):
    store,payload,refs=context
    old=plan(store,'task',literals=[{'role':'footer','quote':'Other source'}])['literals'][0]['ref']
    payload['literal_refs']={'blocks.0.text':refs['paragraph']};payload['blocks'][0]['text']=''
    if inline:payload['footer']['text']={'ref':old}
    else:
        payload['footer']['text']='';payload['literal_refs']['footer.text']=old
    receipt=repair.capture(store,'task',payload)
    before=saved(store)
    with pytest.raises(HarnessError) as caught:
        repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],{'footer.text':refs['footer']})
    assert caught.value.code=='content_conflict' and saved(store)==before
    request=repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],{'footer.text':old})
    assert request.literal_refs=={'blocks.0.text':refs['paragraph']}
    resolved,_=bind_creation(store,'task',request)
    assert resolved.footer.text=='Other source' and resolved.blocks[0].text=='Body source'
    assert repair.get(store,'task')['payload']==payload



@pytest.mark.parametrize('slot',['footer.separator','footer.style','page.margin_twips','blocks.2.text','blocks.1.text',
    'blocks.0.rows.0.0','blocks.1.rows.1.0','blocks.1.rows.0.2','blocks.00.text','blocks.-1.text'])
def test_invalid_slots_fail_atomically(context,slot):
    store,payload,refs=context;receipt=repair.capture(store,'task',payload);before=saved(store)
    with pytest.raises(HarnessError):repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],{slot:refs['footer']})
    assert saved(store)==before


@pytest.mark.parametrize('change',['role','inactive','no_plan','stale','tamper','missing_band'])
def test_invalid_source_or_absent_band_fails_atomically(context,change):
    store,payload,refs=context
    if change=='missing_band':payload.pop('footer')
    receipt=repair.capture(store,'task',payload)
    ref=refs['header'] if change=='role' else refs['footer']
    if change=='inactive':plan(store,'task',literals=[{'role':'header','quote':'Header source'}],plan_mode='replace')
    elif change=='no_plan':
        with store.transaction():store.db.execute('DELETE FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',))
    elif change=='stale':
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    elif change=='tamper':
        with store.transaction():store.db.execute('UPDATE word_content_refs SET body=? WHERE ref=?',('{}',ref))
    before=saved(store)
    with pytest.raises(HarnessError):repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],{'footer.text':ref})
    assert saved(store)==before


@pytest.mark.parametrize('bindings',[[],False,{'x'*101:'ct_'+'0'*24},{'footer.text':'ct_bad'},
    {str(i):'ct_'+'0'*24 for i in range(25)},{1:'ct_'+'0'*24}])
def test_binding_map_bounds(context,bindings):
    store,payload,refs=context;receipt=repair.capture(store,'task',payload);before=saved(store)
    with pytest.raises(HarnessError):repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],bindings)
    assert saved(store)==before


def test_summary_proposed_literals_are_bounded_actual_values_and_refs(context):
    store,payload,refs=context
    payload['header']['text']='😀'*81
    payload['footer']['text']={'ref':refs['footer']}
    for name in ('first_header','first_footer','even_header','even_footer'):payload[name]['text']='界'*80
    original=copy.deepcopy(payload)
    receipt=repair.capture(store,'task',payload)
    navigation=receipt['proposed_literals']
    assert len(canonical(navigation).encode())<=640 and navigation['omitted'] is True
    assert navigation['items'][0]=={'slot':'header.text','preview':'😀'*80,'truncated':True}
    assert {'slot':'footer.text','ref':refs['footer']} in navigation['items']
    assert payload==original and repair.get(store,'task')['payload']==original


def test_summary_short_literal_is_complete_and_style_only_preserves_wrong_text(context):
    store,payload,refs=context;payload['footer']['text']='Page of'
    receipt=repair.capture(store,'task',payload)
    assert {'slot':'footer.text','text':'Page of'} in receipt['proposed_literals']['items']
    request=repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}])
    assert request.footer.text=='Page of'


@pytest.mark.parametrize('value',['Body source',None,'Competing draft','Body source '])
def test_identity_literal_or_identical_inline_only(context,value):
    store,payload,refs=context
    payload['blocks'][0]['text']={'ref':refs['paragraph']} if value is None else value
    receipt=repair.capture(store,'task',payload);before=saved(store)
    if value in ('Competing draft','Body source '):
        with pytest.raises(HarnessError) as caught:
            repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],{'blocks.0.text':refs['paragraph']})
        assert caught.value.code=='content_conflict' and saved(store)==before
    else:
        request=repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],{'blocks.0.text':refs['paragraph']})
        assert request.blocks[0].text.ref==refs['paragraph']


def test_explicit_revision_allows_replacement_then_identity_finalize(context):
    from localoffice.word.creation_revision import revise
    store,payload,refs=context
    receipt=repair.capture(store,'task',payload)
    changed=revise(store,'task',receipt['ref'],[{'op':'set','path':'blocks.0.text','value':'Body source'}])
    request=repair.resolve(store,'task',changed['ref'],[{'id':'Band'}],{'blocks.0.text':refs['paragraph']})
    resolved,_=bind_creation(store,'task',request)
    assert resolved.blocks[0].text=='Body source'
    assert resolved.footer.separator==' of ' and resolved.footer.page_numbers=='current_total'


@pytest.mark.parametrize('text',['One two three','one\u2003two\u00a0three\nfour','word '*100,'😀 '*100,''])
def test_paragraph_word_count_full_text_even_when_preview_truncated(text):
    summary=repair._proposed_literals({'blocks':[{'kind':'paragraph','text':text}]})
    assert summary['word_count_basis']=='unicode_whitespace_split'
    assert summary['items'][0]['word_count']==len(text.split())
    assert len(canonical(summary).encode())<=640
    if len(text)>80:assert summary['items'][0]['truncated'] is True


def test_reference_and_nonparagraph_slots_have_no_inferred_word_count():
    ref='ct_'+'a'*24
    summary=repair._proposed_literals({'header':{'text':'Two words'},'literal_refs':{'blocks.1.text':ref},
        'blocks':[{'kind':'paragraph','text':{'ref':ref}},
                  {'kind':'paragraph','text':''},{'kind':'table','rows':[['Two words']]}]})
    assert len(summary['items'])==4
    assert all('word_count' not in item for item in summary['items'])
    assert len(canonical(summary).encode())<=640


def test_competing_binding_rolls_back_prior_compatible_binding(context):
    store,payload,refs=context
    payload['blocks'][0]['text']='Body source'
    receipt=repair.capture(store,'task',payload);before=saved(store)
    with pytest.raises(HarnessError) as caught:
        repair.resolve(store,'task',receipt['ref'],[{'id':'Band'}],
                       {'blocks.0.text':refs['paragraph'],'footer.text':refs['footer']})
    assert caught.value.code=='content_conflict' and saved(store)==before
