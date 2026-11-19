"""Bounded retained-proposal changes; no worker, model or artifact creation."""
import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.errors import HarnessError
from localoffice.word import creation_repair as repair
from localoffice.word.creation_revision import revise
from localoffice.word.content_plan import plan,bind_creation,ACTIVE_PLAN_PREFIX
from test_word_progress import task


@pytest.fixture
def context(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Exact source' WHERE id='task'")
    source=plan(store,'task',literals=[{'role':'footer','quote':'Exact source'}])['literals'][0]['ref']
    payload={'path':'report.docx','styles':[{'id':'Body','font':'Arial','size_points':11}],
        'page':{'margin_twips':1000},'blocks':[{'kind':'paragraph','text':'Original','style':'Body'},
        {'kind':'table','rows':[['Keep','Cell']]}],
        'footer':{'text':'Wrong','style':'Body','alignment':'center','page_numbers':'current_total','separator':' of '}}
    receipt=repair.capture(store,'task',payload,allow_valid=True)
    yield store,payload,receipt['ref'],source
    store.close()


def snapshot(store):
    return store.db.execute('SELECT value FROM meta WHERE key=?',(repair.PREFIX+'task',)).fetchone()[0]


def test_design_revision_preserves_unselected_payload_and_source_plan(context):
    store,payload,ref,source=context;before=copy.deepcopy(payload)
    active=store.db.execute('SELECT value FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',)).fetchone()[0]
    changes=[{'op':'set','path':'styles.0.size_points','value':9},
        {'op':'set','path':'page.width_twips','value':14000},
        {'op':'set','path':'footer.text','value':{'ref':source}},
        {'op':'insert','path':'blocks.1','value':{'kind':'paragraph','text':'Added'}},
        {'op':'remove','path':'blocks.2.rows.0.1','value':None}]
    original_changes=copy.deepcopy(changes)
    result=revise(store,'task',ref,changes)
    assert result['status']=='prepared' and result['ref']!=ref
    state=repair.get(store,'task');expected=copy.deepcopy(before)
    expected['styles'][0]['size_points']=9;expected['page']['width_twips']=14000
    expected['footer']['text']={'ref':source};expected['blocks'].insert(1,{'kind':'paragraph','text':'Added'})
    expected['blocks'][2]['rows'][0].pop(1)
    assert state['payload']==expected and payload==before and changes==original_changes
    assert state['revision_origin']=={'ref':ref,'payload_hash':digest(canonical(before).encode()),
        'request_hash':digest(canonical({'ref':ref,'changes':changes}).encode())}
    assert store.db.execute('SELECT value FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',)).fetchone()[0]==active
    resolved,_=bind_creation(store,'task',repair.resolve(store,'task',result['ref'],[]))
    assert resolved.footer.text=='Exact source'
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    assert store.candidate('report.docx') is None


def test_exact_replay_and_supersession(context):
    store,payload,ref,source=context
    change=[{'op':'set','path':'footer.alignment','value':'right'}]
    result=revise(store,'task',ref,change);before=snapshot(store)
    assert revise(store,'task',ref,copy.deepcopy(change))==result and snapshot(store)==before
    with pytest.raises(HarnessError):revise(store,'task',ref,[{'op':'set','path':'footer.alignment','value':'left'}])
    second=revise(store,'task',result['ref'],[{'op':'set','path':'footer.alignment','value':'left'}])
    assert second['ref']!=result['ref']
    with pytest.raises(HarnessError):revise(store,'task',ref,change)


def test_noop_revision_still_supersedes_and_clears_resolution(context):
    store,payload,ref,source=context
    repair.resolve(store,'task',ref,[])
    result=revise(store,'task',ref,[{'op':'set','path':'footer.text','value':'Wrong'}])
    assert result['ref']!=ref and repair.get(store,'task')['resolved_request'] is None
    with pytest.raises(HarnessError):repair.get(store,'task',ref)


@pytest.mark.parametrize('change',[
    {'op':'set','path':'tool','value':'word.create'},
    {'op':'set','path':'path','value':'other.docx'},
    {'op':'remove','path':'revision'},
    {'op':'set','path':'blocks.-1.text','value':'No'},
    {'op':'set','path':'blocks.01.text','value':'No'},
    {'op':'set','path':'blocks..text','value':'No'},
    {'op':'set','path':'header.text','value':'No'},
    {'op':'set','path':'blocks.2','value':{}},
    {'op':'insert','path':'blocks.3','value':{}},
    {'op':'insert','path':'page.margin_twips','value':900},
    {'op':'remove','path':'header'},
    {'op':'remove','path':'footer','value':{}},
    {'op':'set','path':'footer.text'},
    {'op':'set','path':'footer.alignment','value':'diagonal'},
    {'op':'set','path':'footer.unknown','value':1},
    {'op':'set','path':'footer.text','value':{'ref':'ct_'+'0'*24}},
])
def test_invalid_change_rolls_back_all_metadata(context,change):
    store,payload,ref,source=context;before=snapshot(store)
    changes=[{'op':'set','path':'footer.text','value':'Interim'},change]
    with pytest.raises((HarnessError,ValidationError)):revise(store,'task',ref,changes)
    assert snapshot(store)==before


def test_explicit_ancestor_then_child_and_array_append(context):
    store,payload,ref,source=context
    result=revise(store,'task',ref,[{'op':'set','path':'header','value':{}},
        {'op':'set','path':'header.text','value':'New header'},
        {'op':'insert','path':'blocks.2','value':{'kind':'paragraph','text':'Last'}},
        {'op':'remove','path':'page'}])
    state=repair.get(store,'task',result['ref'])
    assert state['payload']['header']=={'text':'New header'}
    assert state['payload']['blocks'][-1]['text']=='Last' and 'page' not in state['payload']


@pytest.mark.parametrize('changes',[[],[{'op':'remove','path':'footer'}]*25,
    [{'op':'set','path':'footer.text','value':'😀'*9000}],
    [{'op':'set','path':'footer.text','value':float('nan')}],
    [{'op':'set','path':'footer.text','value':object()}]])
def test_bounds_and_non_json_values_fail_atomically(context,changes):
    store,payload,ref,source=context;before=snapshot(store)
    with pytest.raises(HarnessError):revise(store,'task',ref,changes)
    assert snapshot(store)==before


@pytest.mark.parametrize('mode',['stale','tamper','wrong_chat'])
def test_replay_checks_freshness_hash_and_owner(context,mode):
    from localoffice.chats import create_chat,select_chat
    store,payload,ref,source=context
    changes=[{'op':'remove','path':'footer','value':None}]
    revise(store,'task',ref,changes)
    if mode=='stale':
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    elif mode=='wrong_chat':select_chat(store,create_chat(store))
    else:
        envelope=json.loads(snapshot(store));envelope['state']['revision_origin']['payload_hash']='0'*64
        with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),repair.PREFIX+'task'))
    before=snapshot(store)
    with pytest.raises(HarnessError):revise(store,'task',ref,changes)
    assert snapshot(store)==before


@pytest.mark.parametrize('root',['block_layout','proposed_design','proposed_literals','path','revision','tool'])
def test_invalid_root_has_actionable_registered_correction_without_mutation(context,root):
    store,payload,ref,source=context;before=snapshot(store)
    with pytest.raises(HarnessError) as caught:
        revise(store,'task',ref,[{'op':'set','path':root,'value':False}])
    assert caught.value.code=='creation_revision'
    message=str(caught.value)
    assert 'read-only' in message
    assert 'word.revise_creation' in message and 'blocks, styles, page' in message
    assert 'word.edit_creation_blocks' in message and 'original proposal indices' in message
    assert 'no user confirmation' in message
    assert snapshot(store)==before
