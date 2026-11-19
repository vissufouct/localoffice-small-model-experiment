import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.errors import HarnessError
from localoffice.native_history import record_call,record_result
from localoffice.word import requirements as req,requirement_checks as checks
from localoffice.word.contracts import CreateDocument
from test_word_progress import task

TEXT='Use one-inch margins. Draft eight to sixteen words. Use twelve points. Break before the next paragraph.'

@pytest.fixture
def ctx(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(TEXT,'task'))
    yield store,digest(TEXT.encode())
    store.close()

def entries():
    return [dict(kind='page_geometry',source_quote='one-inch margins',field='margin',equals=1,unit='in'),
        dict(kind='paragraph_words',source_quote='eight to sixteen words',paragraph_index=0,min=8,max=16),
        dict(kind='paragraph_font_size',source_quote='twelve points',paragraph_index=0,equals=12),
        dict(kind='paragraph_page_break',source_quote='Break before the next paragraph',paragraph_index=1,equals=True)]

def document(**kwargs):
    return CreateDocument(path='report.docx',**kwargs)

def test_plan_source_generation_and_empty_history(ctx):
    store,h=ctx;original=entries()
    first=req.plan(store,'task',source_hash=h,requirements=original)
    saved=copy.deepcopy(req.get(store,'task'))
    second=req.plan(store,'task',source_hash=h,requirements=[])
    assert first['status']=='planned' and second['status']=='unassessed'
    assert second['generation']==2 and second['supersedes_generation']==1
    assert original==entries()
    historical=req._read_envelope(store,req.HISTORY_PREFIX+'task:1')
    assert historical==saved
    assert req.get(store,'task')['supersedes']['plan_hash']==digest(canonical(saved).encode())
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_checker_actuals_and_no_plan(ctx):
    store,h=ctx
    doc=document(blocks=[{'kind':'paragraph','text':'Please review this document and share any suggested improvements.'},
                         {'kind':'page_break'},{'kind':'paragraph','text':'End'}],
                 page={'margin_twips':1440},styles=[{'id':'Normal','size_points':12}])
    assert checks.require(store,'task',doc)['status']=='unassessed'
    req.plan(store,'task',source_hash=h,requirements=entries())
    evidence=checks.require(store,'task',doc)
    assert evidence['status']=='all_selected_pass' and evidence['coverage']=='unassessed'
    assert evidence['matched']==4 and len(canonical(evidence).encode())<=640
    wrong=document(blocks=[{'kind':'paragraph','text':'Too short'},{'kind':'paragraph','text':'End'}])
    with pytest.raises(HarnessError) as exc:checks.require(store,'task',wrong)
    assert exc.value.code=='requirements_mismatch'
    assert exc.value.recovery['failed']==4
    page=next(item for item in exc.value.recovery['items'] if item['kind']=='page_geometry')
    assert page['path']=='page.margin_twips' and page['expected']==1440
    assert len(canonical(exc.value.recovery).encode())<=640
    assert exc.value.recovery['omitted_count']>0


@pytest.mark.parametrize('styles,style,expected',[
    ([], 'Normal',11),([{'id':'Normal','size_points':14}], 'Normal',14),
    ([{'id':'Normal','size_points':14},{'id':'Body'}], 'Body',11),
    ([{'id':'Body','size_half_points':25}], 'Body',12.5),
])
def test_effective_worker_font_defaults_no_invented_inheritance(ctx,styles,style,expected):
    store,h=ctx
    entry=dict(kind='paragraph_font_size',source_quote='twelve points',paragraph_index=0,equals=expected)
    req.plan(store,'task',source_hash=h,requirements=[entry])
    doc=document(styles=styles,blocks=[{'kind':'paragraph','text':'Body','style':style}])
    assert checks.require(store,'task',doc)['status']=='all_selected_pass'


def test_body_paragraph_ordinals_skip_table_and_break(ctx):
    store,h=ctx
    req.plan(store,'task',source_hash=h,requirements=[entries()[-1]])
    doc=document(blocks=[{'kind':'paragraph','text':'One'}, {'kind':'table','rows':[['cell']]},
                         {'kind':'page_break'},{'kind':'paragraph','text':'Two'}])
    assert checks.require(store,'task',doc)['status']=='all_selected_pass'
    missing=document(blocks=[{'kind':'paragraph','text':'One'}])
    with pytest.raises(HarnessError) as exc:checks.require(store,'task',missing)
    assert exc.value.code=='requirements_unverified'


def test_stale_even_after_clear_and_readonly(ctx):
    store,h=ctx;req.plan(store,'task',source_hash=h,requirements=[])
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    before=store.db.execute('SELECT key,value FROM meta ORDER BY key').fetchall()
    doc=document(blocks=[{'kind':'paragraph','text':'Body'}])
    with pytest.raises(HarnessError) as exc:checks.require(store,'task',doc)
    assert exc.value.code=='requirements_stale'
    assert store.db.execute('SELECT key,value FROM meta ORDER BY key').fetchall()==before


@pytest.mark.parametrize('quote',['missing',''])
def test_invalid_quote_atomic(ctx,quote):
    store,h=ctx;entry={**entries()[0],'source_quote':quote}
    with pytest.raises((HarnessError,ValidationError)):req.plan(store,'task',source_hash=h,requirements=[entry])
    assert req.get(store,'task') is None


def test_overlapping_quote_ambiguity(ctx):
    store,h=ctx
    with store.transaction():store.db.execute("UPDATE tasks SET objective='aaa' WHERE id='task'")
    with pytest.raises(HarnessError,match='exactly once'):
        req.plan(store,'task',source_hash=digest(b'aaa'),requirements=[{**entries()[0],'source_quote':'aa'}])


@pytest.mark.parametrize('value,unit,expected',[(1,'in',1440),(2.54,'cm',1440),(72,'pt',1440),(1440.5,'twip',1441),(1440.49,'twip',1440)])
def test_exact_conversion_rounding(value,unit,expected):
    assert req.twips(value,unit)==expected


@pytest.mark.parametrize('patch',[{'equals':1e308},{'equals':float('inf')},{'equals':True},{'equals':'1'},{'equals':-1},{'equals':4}])
def test_page_numeric_bounds_are_validation_errors(patch):
    with pytest.raises(ValidationError):req.PlanRequirements(source_hash='a'*64,requirements=[{**entries()[0],**patch}])


@pytest.mark.parametrize('entry',[
    {**entries()[1],'paragraph_index':True},{**entries()[1],'min':20},
    {**entries()[2],'equals':12.25},{**entries()[2],'equals':145},
    {**entries()[3],'equals':1},{**entries()[3],'equals':'true'}])
def test_strict_contract(entry):
    with pytest.raises(ValidationError):req.PlanRequirements(source_hash='a'*64,requirements=[entry])


def test_native_exact_occurrence_history_and_stale_replay(ctx):
    store,h=ctx;args={'source_hash':h,'requirements':entries()}
    action={'tool':'word.plan_requirements',**args}
    first=record_call(store,'task',action)
    receipt=req.plan(store,'task',**args,native_call_id=first)
    record_result(store,'task',first,receipt)
    second=record_call(store,'task',action)
    assert req.result(store,'task',args,native_call_id=second) is None
    newer=req.plan(store,'task',**args,native_call_id=second)
    assert newer['generation']==2
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    assert req.result(store,'task',args,native_call_id=first)==receipt
    assert req.plan(store,'task',**args,native_call_id=first)==receipt
    assert req.get(store,'task')['generation']==2


def test_rehashed_bad_span_detected_on_read(ctx):
    store,h=ctx;req.plan(store,'task',source_hash=h,requirements=entries())
    state=req.get(store,'task');state['source_spans'][0]['start']+=1
    with store.transaction():
        store.db.execute('DELETE FROM meta WHERE key IN (?,?)',(req.PREFIX+'task',req.HISTORY_PREFIX+'task:1'))
        req._write(store,req.HISTORY_PREFIX+'task:1',state)
        req._write(store,req.PREFIX+'task',{'generation':1,'plan_hash':req._hash(state)})
    with pytest.raises(HarnessError,match='integrity'):req.get(store,'task')


def test_wrong_owner_and_target(ctx):
    from localoffice.chats import create_chat,select_chat
    store,h=ctx;req.plan(store,'task',source_hash=h,requirements=entries())
    with pytest.raises(HarnessError):checks.check(store,'task',CreateDocument(path='other.docx',blocks=[{'kind':'paragraph','text':'x'}]))
    select_chat(store,create_chat(store))
    with pytest.raises(HarnessError):req.get(store,'task')


def test_prepared_preview_no_plan_and_clear(ctx):
    store,h=ctx
    assert checks.check_prepared(store,'task',{}) is None
    req.plan(store,'task',source_hash=h,requirements=[])
    assert checks.check_prepared(store,'task',{}) is None


@pytest.mark.parametrize('payload',[
    {'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body','style':'Missing'}]},
    {'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'},{'kind':'page_break'}]},
    {'path':'report.docx','blocks':[{'kind':'paragraph','text':{'ref':'ct_'+'0'*24}}]},
])
def test_prepared_invalid_unverified_and_stale_preempts(ctx,payload):
    store,h=ctx;req.plan(store,'task',source_hash=h,requirements=[entries()[0]])
    before=store.db.execute('SELECT key,value FROM meta ORDER BY key').fetchall()
    evidence=checks.check_prepared(store,'task',payload)
    assert evidence['status']=='unverified' and evidence['reason']=='current_proposal_not_ready'
    assert store.db.execute('SELECT key,value FROM meta ORDER BY key').fetchall()==before
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    assert checks.check_prepared(store,'task',payload)['status']=='stale'


def test_prepared_preview_resolves_actual_source_text(ctx):
    from localoffice.word.content_plan import plan
    store,h=ctx
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Use twelve points.'}])['literals'][0]['ref']
    req.plan(store,'task',source_hash=h,requirements=[{
        'kind':'paragraph_words','source_quote':'eight to sixteen words','paragraph_index':0,'min':3,'max':3}])
    payload={'path':'report.docx','blocks':[{'kind':'paragraph','text':{'ref':ref}}]}
    original=copy.deepcopy(payload)
    assert checks.check_prepared(store,'task',payload)['status']=='all_selected_pass'
    assert payload==original
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


def test_native_receipt_insert_failure_rolls_back_history(ctx):
    store,h=ctx;args={'source_hash':h,'requirements':entries()}
    call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
    with store.transaction():
        store.db.execute("CREATE TRIGGER reject_requirements BEFORE INSERT ON meta WHEN NEW.key LIKE 'word_requirements_result:%' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(Exception,match='interrupted'):req.plan(store,'task',**args,native_call_id=call)
    assert req.get(store,'task') is None
    assert store.db.execute('SELECT count(*) FROM meta WHERE key LIKE ?', (req.HISTORY_PREFIX+'%',)).fetchone()[0]==0


@pytest.mark.parametrize('mode',['tool','args','unknown','completed'])
def test_native_identity_checks_before_mutation(ctx,mode):
    store,h=ctx;args={'source_hash':h,'requirements':entries()}
    action={'tool':'word.plan_requirements',**args}
    if mode=='tool':action['tool']='word.create'
    if mode=='args':action['requirements']=[]
    call=record_call(store,'task',action)
    if mode=='unknown':call='call_'+'0'*32
    if mode=='completed':record_result(store,'task',call,{'status':'other'})
    with pytest.raises(HarnessError):req.plan(store,'task',**args,native_call_id=call)
    assert req.get(store,'task') is None


def test_whitespace_and_direct_break(ctx):
    store,h=ctx
    selected=[{**entries()[1],'min':3,'max':3},{**entries()[-1],'paragraph_index':0}]
    req.plan(store,'task',source_hash=h,requirements=selected)
    doc=document(blocks=[{'kind':'paragraph','text':'one\u00a0two\u2003three','page_break_before':True}])
    assert checks.require(store,'task',doc)['status']=='all_selected_pass'


def test_quote_diagnostics_preserve_prior_plan_and_report_all_indices(ctx):
    store,h=ctx
    req.plan(store,'task',source_hash=h,requirements=entries())
    before=[tuple(row) for row in store.db.execute('SELECT key,value FROM meta ORDER BY key')]
    invalid=entries()
    invalid[1]['source_quote']='eight\\ to sixteen words'
    invalid[3]['source_quote']='Use'
    with pytest.raises(HarnessError) as exc:
        req.plan(store,'task',source_hash=h,requirements=invalid)
    assert exc.value.code=='requirements_quote'
    assert exc.value.recovery['invalid_quotes']==[
        {'index':1,'reason':'not_found'},{'index':3,'reason':'ambiguous'}]
    assert exc.value.recovery['index_base']==0
    assert [tuple(row) for row in store.db.execute('SELECT key,value FROM meta ORDER BY key')]==before


def test_quote_diagnostics_bounded_for_eight_large_quotes(ctx):
    store,h=ctx
    invalid=[{**entries()[0],'source_quote':'x'*3000} for _ in range(8)]
    with pytest.raises(HarnessError) as exc:
        req.plan(store,'task',source_hash=h,requirements=invalid)
    assert len(exc.value.recovery['invalid_quotes'])==8
    assert len(canonical(exc.value.recovery).encode())<=640
    assert req.get(store,'task') is None


def test_failure_only_feedback_has_actual_text_path_after_standalone_break(ctx):
    store,h=ctx
    declarations=[entries()[0],{**entries()[1],'paragraph_index':1}]
    req.plan(store,'task',source_hash=h,requirements=declarations)
    before=copy.deepcopy(req.get(store,'task'))
    doc=document(page={'margin_twips':1440},blocks=[
        {'kind':'paragraph','text':'First'}, {'kind':'page_break'},
        {'kind':'paragraph','text':'Too short'}])
    result=checks.check(store,'task',doc)
    assert result['matched']==1 and result['failed']==1
    assert result['items_scope']=='failures' and result['omitted_count']==0
    assert len(result['items'])==1
    item=result['items'][0]
    assert item['paragraph_index']==1 and item['block_index']==2
    assert item['path']=='blocks.2.text' and item['actual']==2
    assert item['expected']=={'min':8,'max':16}
    assert req.get(store,'task')==before
    assert len(canonical(result).encode())<=640
