"""Retained valid proposals: pure host state, no worker/model/render."""
import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.word import creation_repair as repair
from localoffice.word.content_plan import plan,bind_creation
from test_word_progress import task


@pytest.fixture
def context(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Body; Page ' WHERE id='task'")
    payload={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}],
             'footer':{'text':'Page of','alignment':'center','page_numbers':'current_total','separator':' of '}}
    yield store,payload
    store.close()


def test_valid_prepare_reuse_resolve_and_no_document_mutation(context):
    store,payload=context;original=copy.deepcopy(payload)
    assert repair.capture(store,'task',payload) is None
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    assert receipt['status']=='prepared' and receipt['missing_styles']==[]
    assert receipt['document_changed'] is False
    assert repair.capture(store,'task',payload,allow_valid=True)==receipt
    assert repair.pending(store,'task')==receipt
    state=repair.get(store,'task');assert state['review_required'] is True
    request=repair.resolve(store,'task',receipt['ref'],[])
    assert request.footer.text=='Page of'
    assert repair.get(store,'task')['payload']==payload==original
    assert store.candidate('report.docx') is None
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_replacement_invalidates_old_handle_and_cannot_drift_existing_styles(context):
    store,payload=context
    first=repair.capture(store,'task',payload,allow_valid=True)
    changed=copy.deepcopy(payload);changed['styles']=[{'id':'Normal','size_points':9}]
    second=repair.capture(store,'task',changed,allow_valid=True)
    assert first['ref']!=second['ref']
    with pytest.raises(HarnessError):repair.resolve(store,'task',first['ref'],[])
    with pytest.raises(HarnessError):repair.resolve(store,'task',second['ref'],[{'id':'Normal','size_points':11}])
    assert repair.resolve(store,'task',second['ref'],[]).styles[0].size_points==9


def test_review_marker_changes_handle_for_same_missing_style_payload(context):
    store,payload=context;payload['footer']['style']='Band'
    old=repair.capture(store,'task',payload)
    prepared=repair.capture(store,'task',payload,allow_valid=True)
    assert prepared['ref']!=old['ref'] and prepared['status']=='prepared'
    assert prepared['missing_styles']==old['missing_styles']
    with pytest.raises(HarnessError):repair.resolve(store,'task',prepared['ref'],[])
    assert repair.resolve(store,'task',prepared['ref'],[{'id':'Band'}]).footer.style=='Band'
    back=repair.capture(store,'task',payload)
    assert back['ref']!=prepared['ref'] and back['status']=='missing_styles'


@pytest.mark.parametrize('change',['unknown','bad_break','bad_source','bad_slot'])
def test_prepare_propagates_validation_and_source_errors_atomically(context,change):
    store,payload=context
    valid=repair.capture(store,'task',payload,allow_valid=True)
    before=repair.get(store,'task')
    bad=copy.deepcopy(payload)
    if change=='unknown':bad['unexpected']=True
    elif change=='bad_break':bad['blocks']=[{'kind':'page_break'}]
    elif change=='bad_source':bad['footer']['text']={'ref':'ct_'+'0'*24}
    else:bad['literal_refs']={'blocks.9.text':'ct_'+'0'*24}
    if change=='bad_break':
        retained=repair.capture(store,'task',bad,allow_valid=True)
        assert retained['status']=='invalid_design'
        assert retained['document_changed'] is False
        assert retained['ref']!=valid['ref']
        assert repair.get(store,'task')['payload']==bad
        assert retained['validation_errors']['items']
        with pytest.raises((ValidationError,HarnessError)):
            repair.resolve(store,'task',retained['ref'],[])
        assert store.candidate('report.docx') is None
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        return
    with pytest.raises((ValidationError,HarnessError)):repair.capture(store,'task',bad,allow_valid=True)
    assert repair.get(store,'task')==before
    assert repair.get(store,'task')['ref']==valid['ref']


def test_valid_proposal_explicit_binding_and_unselected_future_refs(context):
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'footer','quote':'Page '},{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
    payload['blocks'][0]['text']='Draft introduction'
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    with pytest.raises(HarnessError) as caught:
        repair.resolve(store,'task',receipt['ref'],[],{'footer.text':ref})
    assert caught.value.code=='content_conflict'
    from localoffice.word.creation_revision import revise
    revised=revise(store,'task',receipt['ref'],[{'op':'set','path':'footer.text','value':'Page '}])
    request=repair.resolve(store,'task',revised['ref'],[],{'footer.text':ref})
    resolved,_=bind_creation(store,'task',request)
    assert resolved.footer.text=='Page ' and resolved.blocks[0].text=='Draft introduction'
    assert repair.get(store,'task')['payload']['footer']['text']=='Page '


def test_proposal_design_reports_actual_values_and_contract_defaults(context):
    store,payload=context
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    items=receipt['proposed_design']['items']
    assert receipt['proposed_design']['style_count']==0
    assert 'Use revise_creation for compact content/design changes; prepare_creation replaces the whole proposal.' in receipt['instruction']
    assert {'style':'Normal','font':'Arial','size_points':11,'implicit_default':True} in items
    band=next(item for item in items if item.get('slot')=='footer')
    assert band['style']=='Normal' and band['alignment']=='center'
    assert band['page_numbers']=='current_total' and band['separator']==' of '
    assert receipt['proposed_design']['presence']=='unknown'
    assert 'defaults' not in band
    # A band-free proposal leaves enough room for complete geometry evidence.
    payload.pop('footer')
    design=repair.capture(store,'task',payload,allow_valid=True)['proposed_design']
    page=next(item for item in design['items'] if 'page_twips' in item)
    assert page['page_twips']['width_twips']==11906 and page['page_twips']['height_twips']==16838
    assert design['presence']=='unknown' and 'defaults' not in page


def test_explicit_font_and_half_point_size_are_not_reported_as_defaults(context):
    store,payload=context;payload.pop('footer')
    payload['styles']=[{'id':'Normal','font':'Calibri','size_half_points':19}]
    design=repair.capture(store,'task',payload,allow_valid=True)['proposed_design']
    assert {'slot':'styles.0','style':'Normal','font':'Calibri','size_points':9.5} in design['items']
    assert design['style_count']==1
    assert not any(item.get('implicit_default') for item in design['items'])


def test_bounded_unicode_design_and_literals_leave_payload_unchanged(context):
    store,payload=context
    payload['styles']=[{'id':f'Style{i}','font':'界'*80,'size_points':10} for i in range(16)]
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        payload[name]={'text':'😀'*100,'style':'Style0','separator':'界'*40,'page_numbers':'current_total'}
    before=copy.deepcopy(payload)
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    for key in ('proposed_literals','proposed_design'):
        assert len(canonical(receipt[key]).encode())<=640
        assert receipt[key]['omitted'] is True
    assert payload==before and repair.get(store,'task')['payload']==before


def test_prepared_freshness_and_hash_integrity_remain_required(context):
    store,payload=context
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    with pytest.raises(HarnessError):repair.resolve(store,'task',receipt['ref'],[])
    key=repair.PREFIX+'task'
    envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
    envelope['state']['review_required']=False
    with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),key))
    with pytest.raises(HarnessError,match='integrity'):repair.get(store,'task')


def warning_items(receipt):
    return [item for item in receipt['proposed_literals']['items'] if item.get('reference_like')]


def test_reference_like_active_handle_warns_without_rejection_or_rewriting(context):
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
    payload['blocks'][0]['text']='ref: '+ref
    before=copy.deepcopy(payload)
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    assert warning_items(receipt)==[{'slot':'blocks.0.text','text':'ref: '+ref,'word_count':2,'reference_like':True}]
    assert 'otherwise retain it' in receipt['instruction']
    assert len(receipt['instruction'])<=270
    assert len(canonical(receipt['proposed_literals']).encode())<=640
    assert repair.resolve(store,'task',receipt['ref'],[]).blocks[0].text=='ref: '+ref
    assert payload==before and repair.get(store,'task')['payload']==before


@pytest.mark.parametrize('form',['unknown','prose','bare','spacing','inactive','no_plan'])
def test_nonmatching_or_inactive_reference_text_is_not_warned(context,form):
    from localoffice.word.content_plan import ACTIVE_PLAN_PREFIX
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
    text='ref: '+ref
    if form=='unknown':text='ref: ct_'+'0'*24
    elif form=='prose':text='Documentation shows '+text
    elif form=='bare':text=ref
    elif form=='spacing':text='ref:  '+ref
    elif form=='inactive':plan(store,'task',literals=[{'role':'footer','quote':'Page '}],plan_mode='replace')
    elif form=='no_plan':
        with store.transaction():store.db.execute('DELETE FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',))
    payload['blocks'][0]['text']=text
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    assert not warning_items(receipt)
    assert repair.resolve(store,'task',receipt['ref'],[]).blocks[0].text==text


def test_same_role_explicit_source_literal_exempts_reference_like_text(context):
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'footer','quote':'Page '}])['literals'][0]['ref']
    text='ref: '+ref
    message=store.message('user',text,'task')
    literal=plan(store,'task',source_message=message,literals=[{'role':'footer','quote':text}])['literals'][0]['ref']
    payload['footer']['text']=text
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    assert not warning_items(receipt)
    # Source-backed literal remains exempt when explicitly bound too.
    request=repair.resolve(store,'task',receipt['ref'],[],{'footer.text':literal})
    assert bind_creation(store,'task',request)[0].footer.text==text


@pytest.mark.parametrize('inline',[False,True])
def test_explicit_binding_entries_remain_refs_without_warning(context,inline):
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
    if inline:payload['blocks'][0]['text']={'ref':ref}
    else:payload['literal_refs']={'blocks.0.text':ref}
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    assert {'slot':'blocks.0.text','ref':ref} in receipt['proposed_literals']['items']
    assert not warning_items(receipt)


def test_warning_rechecks_current_membership_and_stale_sources(context):
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
    payload['blocks'][0]['text']='ref: '+ref
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    assert warning_items(receipt)
    before=repair.get(store,'task')
    plan(store,'task',literals=[{'role':'footer','quote':'Page '}],plan_mode='replace')
    assert not warning_items(repair.pending(store,'task'))
    assert repair.get(store,'task')==before
    # A new, fresh proposal may coexist with stale historical plan sources;
    # those sources cannot justify a current-handle warning.
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    payload['footer']['text']='ref: '+plan_ref_from_state(store)
    fresh=repair.capture(store,'task',payload,allow_valid=True)
    assert not warning_items(fresh)


def plan_ref_from_state(store):
    from localoffice.word.content_plan import ACTIVE_PLAN_PREFIX
    envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',)).fetchone()[0])
    return envelope['state']['refs'][0]['ref']


def test_many_warning_entries_stay_byte_bounded_and_do_not_change_state(context):
    store,payload=context
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])['literals'][0]['ref']
    payload['blocks']=[{'kind':'paragraph','text':'ref: '+ref} for _ in range(24)]
    payload['header']={'text':'😀'*80}
    result=repair.capture(store,'task',payload,allow_valid=True)
    assert warning_items(result)
    assert len(canonical(result['proposed_literals']).encode())<=640
    assert result['proposed_literals']['omitted'] is True
    before=repair.get(store,'task')
    assert repair.pending(store,'task')==result and repair.get(store,'task')==before


@pytest.mark.parametrize('kind',['prepared','invalid_design','missing_styles'])
def test_summary_readonly_hint_preserves_status_and_facts(context,kind):
    store,payload=context
    if kind=='invalid_design':payload['blocks'].append({'kind':'page_break'})
    if kind=='missing_styles':payload['blocks'][0]['style']='Missing'
    receipt=repair.capture(store,'task',payload,allow_valid=kind!='missing_styles')
    assert receipt['status']==kind
    assert receipt['instruction'].endswith('Summary fields are read-only; edit blocks/styles/page via tools.')
    assert receipt['document_changed'] is False
    assert receipt['proposed_literals']['items']
    assert repair.get(store,'task')['payload']==payload
    if kind=='missing_styles':
        assert 'Declare exactly the missing style IDs' in receipt['instruction']
        assert 'confirm identical text or fill empty slots' in receipt['instruction']
