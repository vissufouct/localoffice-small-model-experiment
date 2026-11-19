"""Bounded resolver diagnostics identify failures without guessing bindings."""
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import canonical
from localoffice.word import creation_repair as repair
from localoffice.word.content_plan import plan, bind_creation
from test_creation_repair_bindings import context, saved


@pytest.mark.parametrize('slot,expected,received',[
    ('even_footer.text','footer','header'),
    ('first_header.text','header','footer'),
    ('blocks.0.text','paragraph','table_cell'),
    ('blocks.1.rows.0.0','table_cell','paragraph'),
])
def test_role_mismatch_exact_slot_and_roles_atomic_no_guessed_reference(context,slot,expected,received):
    store,payload,refs=context
    payload['header']['text']='Header source'
    hint=repair.capture(store,'task',payload);before=saved(store)
    with pytest.raises(HarnessError) as error:
        repair.resolve(store,'task',hint['ref'],[{'id':'Band'}],
                       {'header.text':refs['header'],slot:refs[received]})
    exc=error.value
    assert exc.code=='content_reference'
    assert exc.recovery['slot']==slot
    assert exc.recovery['expected_role']==expected
    assert exc.recovery['received_role']==received
    assert exc.recovery['reason']=='role_mismatch'
    assert slot in str(exc) and expected in str(exc) and received in str(exc)
    assert 'active reference' in exc.recovery['correction']
    assert 'Do not ask the user for internal IDs' in exc.recovery['correction']
    assert 'ct_' not in canonical(exc.recovery)
    assert len(canonical({'message':str(exc),'recovery':exc.recovery}).encode())<=640
    assert saved(store)==before
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_inactive_reference_not_misreported_as_other_role(context):
    store,payload,refs=context;hint=repair.capture(store,'task',payload)
    plan(store,'task',plan_mode='replace',literals=[{'role':'header','quote':'Header source'}])
    before=saved(store)
    with pytest.raises(HarnessError) as error:
        repair.resolve(store,'task',hint['ref'],[{'id':'Band'}],{'footer.text':refs['footer']})
    assert error.value.code=='content_reference'
    assert error.value.recovery['reason']=='inactive_reference'
    assert error.value.recovery['expected_role']=='footer'
    assert error.value.recovery['received_role'] is None
    assert saved(store)==before


@pytest.mark.parametrize('slot',['footer.style','blocks.99.text','界'*100,'\x01'*100])
def test_invalid_slot_exact_bounded_diagnostic_no_invented_role(context,slot):
    store,payload,refs=context;hint=repair.capture(store,'task',payload);before=saved(store)
    with pytest.raises(HarnessError) as error:
        repair.resolve(store,'task',hint['ref'],[{'id':'Band'}],{slot:refs['footer']})
    exc=error.value
    assert exc.code=='content_slot'
    assert exc.recovery['slot']==slot
    assert exc.recovery['expected_role'] is None
    assert exc.recovery['received_role']=='footer'
    assert exc.recovery['reason']=='invalid_slot'
    assert len(canonical({'message':str(exc),'recovery':exc.recovery}).encode())<=1024
    assert saved(store)==before


def test_stale_source_preempts_binding_diagnostic(context):
    store,payload,refs=context;hint=repair.capture(store,'task',payload);before=saved(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    with pytest.raises(HarnessError) as error:
        repair.resolve(store,'task',hint['ref'],[{'id':'Band'}],{'even_footer.text':refs['header']})
    assert error.value.code=='creation_repair_stale'
    assert error.value.recovery is None
    assert saved(store)==before


def test_empty_slot_explicit_same_role_choice_not_autopaired(context):
    store,payload,refs=context
    other=plan(store,'task',literals=[{'role':'header','quote':'Other source'}])['literals'][0]['ref']
    payload['header']['text']=''
    hint=repair.capture(store,'task',payload)
    selected=repair.resolve(store,'task',hint['ref'],[{'id':'Band'}],{'header.text':other})
    resolved,_=bind_creation(store,'task',selected)
    assert resolved.header.text=='Other source'
    assert resolved.first_header.text==payload['first_header']['text']
    assert repair.get(store,'task')['payload']==payload


def test_competing_same_role_content_requires_explicit_revision(context):
    from localoffice.word.creation_revision import revise
    store,payload,refs=context
    hint=repair.capture(store,'task',payload);before=saved(store)
    with pytest.raises(HarnessError) as error:
        repair.resolve(store,'task',hint['ref'],[{'id':'Band'}],{'header.text':refs['header']})
    assert error.value.code=='content_conflict'
    assert error.value.recovery['slot']=='header.text'
    assert error.value.recovery['expected_role']=='header'
    assert 'revise_creation' in error.value.recovery['correction']
    assert 'ct_' not in canonical(error.value.recovery)
    assert saved(store)==before
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    changed=revise(store,'task',hint['ref'],[{'op':'set','path':'header.text','value':'Header source'}])
    selected=repair.resolve(store,'task',changed['ref'],[{'id':'Band'}],{'header.text':refs['header']})
    resolved,_=bind_creation(store,'task',selected)
    assert resolved.header.text=='Header source'
    assert resolved.first_header.text==payload['first_header']['text']
