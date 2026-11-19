import json
import pytest
from localoffice.storage import ProjectStore,canonical
from localoffice.native_history import record_call,record_result
from localoffice.errors import HarnessError
from localoffice.word import creation_submission as sub,creation_repair as repair
from localoffice.word.plugin import WordPlugin
from localoffice.word.creation_revision import revise
from test_word_progress import task

@pytest.fixture
def ctx(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    yield store,{'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body','page_break_before':False}]}
    store.close()

def execute(store,raw):
    call=record_call(store,'task',{'tool':'word.prepare_creation',**raw})
    receipt=WordPlugin(store).prepare_creation(task_id='task',native_call_id=call,**raw)
    return call,receipt

def test_same_normalized_raw_presence_and_immutable_receipts(ctx):
    store,raw=ctx
    a,first=execute(store,raw);state=repair.get(store,'task');payload_hash=state['payload_hash']
    assert 'page.margin_twips' not in sub.presence(store,state)
    assert 'blocks.0.page_break_before' in sub.presence(store,state)
    assert first['proposed_design']['presence']=='verified'
    page=next(i for i in first['proposed_design']['items'] if 'page_twips' in i)
    assert 'margin_twips' in page['defaults']
    record_result(store,'task',a,first)
    explicit={**raw,'page':{'margin_twips':1134}}
    b=record_call(store,'task',{'tool':'word.prepare_creation',**explicit})
    assert sub.result(store,'task',native_call_id=b) is None
    second=WordPlugin(store).prepare_creation(task_id='task',native_call_id=b,**explicit)
    assert second['ref']==first['ref'] and repair.get(store,'task')['payload_hash']==payload_hash
    assert 'page.margin_twips' in sub.presence(store,repair.get(store,'task'))
    assert sub.result(store,'task',native_call_id=a)==first
    assert sub.result(store,'task',native_call_id=b)==second
    page=next(i for i in second['proposed_design']['items'] if 'page_twips' in i)
    assert 'margin_twips' not in page['defaults']
    assert len(canonical(second['proposed_design']).encode())<=640

def test_unknown_and_revised_presence(ctx):
    store,raw=ctx
    receipt=WordPlugin(store).prepare_creation(task_id='task',**raw)
    assert receipt['proposed_design']['presence']=='unknown'
    assert all('defaults' not in i for i in receipt['proposed_design']['items'])
    call,receipt=execute(store,raw)
    revised=revise(store,'task',receipt['ref'],[{'op':'set','path':'blocks.0.text','value':'Body'}])
    assert revised['proposed_design']['presence']=='unknown'
    assert sub.presence(store,repair.get(store,'task')) is None
    assert sub.result(store,'task',native_call_id=call)==receipt

@pytest.mark.parametrize('mode',['args','owner','completed','target','tool'])
def test_verification_rejects_without_capture(ctx,mode):
    from localoffice.chats import create_chat,select_chat
    store,raw=ctx
    action={'tool':'word.prepare_creation',**raw}
    if mode=='tool':action['tool']='word.create'
    if mode=='target':action['path']='other.docx'
    call=record_call(store,'task',action)
    if mode=='completed':record_result(store,'task',call,{'status':'other'})
    if mode=='owner':select_chat(store,create_chat(store))
    if mode=='args':raw={**raw,'page':{'margin_twips':1440}}
    with pytest.raises(HarnessError):WordPlugin(store).prepare_creation(task_id='task',native_call_id=call,**raw)
    assert store.db.execute('SELECT count(*) FROM meta WHERE key LIKE ?', (sub.PREFIX+'%',)).fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM meta WHERE key=?',(repair.PREFIX+'task',)).fetchone()[0]==0

def test_atomic_failure(ctx):
    store,raw=ctx
    call=record_call(store,'task',{'tool':'word.prepare_creation',**raw})
    with store.transaction():
        store.db.execute("CREATE TRIGGER no_submission BEFORE INSERT ON meta WHEN NEW.key LIKE 'word_creation_submission:%' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(Exception,match='interrupted'):
        WordPlugin(store).prepare_creation(task_id='task',native_call_id=call,**raw)
    assert repair.get(store,'task') is None

def test_tamper_and_historical_replay(ctx):
    store,raw=ctx;call,receipt=execute(store,raw)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    assert sub.result(store,'task',native_call_id=call)==receipt
    assert WordPlugin(store).prepare_creation(task_id='task',native_call_id=call,**raw)==receipt
    key=sub.PREFIX+call
    envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
    envelope['state']['verified']['provided_fields'].append('page.margin_twips')
    with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),key))
    with pytest.raises(HarnessError,match='integrity'):sub.result(store,'task',native_call_id=call)

def test_nested_explicit_default_font_size_and_false(ctx):
    store,raw=ctx
    raw={**raw,'styles':[{'id':'Body','font':'Arial','size_points':11}],
         'header':{'text':'Label','alignment':'left','style':'Normal'}}
    _,receipt=execute(store,raw)
    paths=sub.presence(store,repair.get(store,'task'))
    assert {'styles.0.font','styles.0.size_points','header.alignment','blocks.0.page_break_before'}<=paths
    style=next(i for i in receipt['proposed_design']['items'] if i.get('slot')=='styles.0')
    assert 'defaults' not in style
    band=next(i for i in receipt['proposed_design']['items'] if i.get('slot')=='header')
    assert 'alignment' not in band.get('defaults',[])
    assert 'page_numbers' in band['defaults']

def test_unknown_submission_clears_previous_marker_without_changing_old_receipt(ctx):
    store,raw=ctx;call,receipt=execute(store,raw)
    direct=WordPlugin(store).prepare_creation(task_id='task',**raw)
    assert direct['ref']==receipt['ref'] and direct['proposed_design']['presence']=='unknown'
    assert sub.presence(store,repair.get(store,'task')) is None
    assert sub.result(store,'task',native_call_id=call)==receipt

def test_b1_geometry_survives_named_style_and_implicit_normal(ctx):
    store,raw=ctx
    raw={**raw,'styles':[{'id':'Body','font':'Arial','size_points':12}],
         'blocks':[{'kind':'paragraph','text':'Body','style':'Body'}]}
    _,receipt=execute(store,raw)
    design=receipt['proposed_design']
    assert len(canonical(design).encode())<=640
    page=next(item for item in design['items'] if 'page_twips' in item)
    assert page['page_twips']['margin_twips']==1134
    assert set(page['defaults'])==set(page['page_twips'])
    assert any(item.get('slot')=='styles.0' for item in design['items'])
