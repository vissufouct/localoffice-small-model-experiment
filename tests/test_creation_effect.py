import json
import pytest
from localoffice.storage import canonical
from localoffice.native_history import record_call,record_result
from localoffice.word.creation_effect import summarize
from localoffice.word.creation_revision import revise
from localoffice.word import creation_repair as repair,creation_blocks,creation_submission
from localoffice.word.plugin import WordPlugin
from test_creation_revision import context,snapshot


def test_net_noop_and_presence():
    before={'blocks':[{'text':'Same'}]}
    assert summarize(before,before,[{'op':'set','path':'blocks.0.text','value':'Same'}])=={
        'scope':'proposal_data_only','proposal_changed':False,'changed':[],
        'unchanged':['blocks.0.text'],'omitted_count':0}
    after={'blocks':[{'text':'Same','page_break_before':False}]}
    effect=summarize(before,after,[{'op':'set','path':'blocks.0.page_break_before','value':False}])
    assert effect['proposal_changed'] and effect['changed']==['blocks.0.page_break_before']


def test_cancelled_revisions_exact_replay(context):
    store,_,ref,_=context
    changes=[{'op':'set','path':'blocks.0.text','value':'Temporary'},
             {'op':'set','path':'blocks.0.text','value':'Original'}]
    receipt=revise(store,'task',ref,changes)
    assert receipt['ref']!=ref
    assert receipt['edit_effect']['proposal_changed'] is False
    assert receipt['edit_effect']['unchanged']==['blocks.0.text']
    before=snapshot(store)
    assert revise(store,'task',ref,changes)==receipt and snapshot(store)==before


def test_structural_shift_collapses_array(context):
    store,_,ref,_=context
    receipt=revise(store,'task',ref,[{'op':'remove','path':'blocks.0','value':None}])
    assert receipt['edit_effect']['changed']==['blocks']
    assert receipt['edit_effect']['unchanged']==[]


def test_typed_snapshot_effect_and_per_call_receipt(context):
    store,_,ref,_=context
    args={'ref':ref,'changes':[{'op':'remove_block','index':1},
                             {'op':'set_paragraph','index':0,'text':'New'}]}
    call=record_call(store,'task',{'tool':'word.edit_creation_blocks',**args})
    receipt=creation_blocks.edit(store,'task',**args,native_call_id=call)
    assert receipt['edit_effect']['changed']==['blocks']
    assert creation_blocks.result(store,'task',args,native_call_id=call)==receipt


def test_utf8_bound_complete_omission_count():
    before={str(i)+'界'*40:0 for i in range(24)}
    after={key:1 for key in before}
    effect=summarize(before,after,[{'op':'set','path':k,'value':1} for k in before])
    assert len(canonical(effect).encode())<=256
    assert effect['proposal_changed'] is True
    assert len(effect['changed'])+effect['omitted_count']==24


def test_fresh_prepare_same_payload_hides_effect_preserves_history(context):
    store,_,_,_=context
    first=WordPlugin(store).prepare_creation(task_id='task',path='report.docx',
        blocks=[{'kind':'paragraph','text':'Original'}])
    args={'ref':first['ref'],'changes':[{'op':'set_paragraph','index':0,'text':'New'}]}
    call=record_call(store,'task',{'tool':'word.edit_creation_blocks',**args})
    revised=creation_blocks.edit(store,'task',**args,native_call_id=call)
    record_result(store,'task',call,revised)
    original_state=repair.get(store,'task')
    raw={'path':'report.docx','blocks':[{'kind':'paragraph','text':'New'}]}
    prepare_call=record_call(store,'task',{'tool':'word.prepare_creation',**raw})
    fresh=WordPlugin(store).prepare_creation(task_id='task',native_call_id=prepare_call,**raw)
    assert fresh['ref']==revised['ref'] and 'edit_effect' not in fresh
    state=repair.get(store,'task')
    assert state['revision_origin']==original_state['revision_origin']
    assert state['block_revision_origin']==original_state['block_revision_origin']
    assert creation_blocks.result(store,'task',args,native_call_id=call)==revised
    assert creation_submission.result(store,'task',native_call_id=prepare_call)==fresh


def test_array_replacement_reports_array_not_shifted_children():
    before={'blocks':[{'text':'A'},{'text':'B'}]}
    after={'blocks':[{'text':'B edited'},{'text':'A'}]}
    effect=summarize(before,after,[{'op':'set','path':'blocks','value':after['blocks']}])
    assert effect['changed']==['blocks']
    assert effect['unchanged']==[]


def test_numeric_and_boolean_are_distinct_stored_values():
    effect=summarize({'value':False},{'value':0},[{'op':'set','path':'value','value':0}])
    assert effect['proposal_changed'] and effect['changed']==['value']


def test_generic_original_receipt_survives_fresh_prepare(context):
    from localoffice.word.creation_revision import result
    store,_,_,_=context
    first=WordPlugin(store).prepare_creation(task_id='task',path='report.docx',
        blocks=[{'kind':'paragraph','text':'Original'}])
    changes=[{'op':'set','path':'blocks.0.text','value':'New'}]
    revised=revise(store,'task',first['ref'],changes)
    fresh=WordPlugin(store).prepare_creation(task_id='task',path='report.docx',
        blocks=[{'kind':'paragraph','text':'New'}])
    assert fresh['ref']==revised['ref'] and 'edit_effect' not in fresh
    before=snapshot(store)
    assert revise(store,'task',first['ref'],changes)==revised
    assert result(store,'task',first['ref'],changes)==revised
    assert snapshot(store)==before
    assert 'edit_effect' not in repair.pending(store,'task')


def test_typed_direct_noop_replay_after_fresh_prepare_keeps_original_effect(context):
    store,_,_,_=context
    raw={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Original'}]}
    first=WordPlugin(store).prepare_creation(task_id='task',**raw)
    args={'ref':first['ref'],'changes':[{'op':'set_paragraph','index':0,'text':'Original'}]}
    revised=creation_blocks.edit(store,'task',**args)
    assert revised['ref']!=first['ref']
    assert revised['edit_effect']['proposal_changed'] is False
    assert revised['edit_effect']['unchanged']==['blocks.0.text']
    fresh=WordPlugin(store).prepare_creation(task_id='task',**raw)
    assert fresh['ref']==revised['ref'] and 'edit_effect' not in fresh
    before=snapshot(store)
    assert creation_blocks.edit(store,'task',**args)==revised
    assert snapshot(store)==before
    assert 'edit_effect' not in repair.pending(store,'task')
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    assert store.candidate('report.docx') is None


def test_native_generic_recovery_after_fresh_prepare_keeps_original_effect(context):
    from localoffice.native_history import reconcile_word_call,exchanges
    store,_,_,_=context
    raw={'path':'report.docx','blocks':[{'kind':'paragraph','text':'Original'}]}
    first=WordPlugin(store).prepare_creation(task_id='task',**raw)
    changes=[{'op':'set','path':'blocks.0.text','value':'New'}]
    action={'tool':'word.revise_creation','ref':first['ref'],'changes':changes}
    call=record_call(store,'task',action)
    revised=revise(store,'task',first['ref'],changes)
    assert revised['edit_effect']['proposal_changed'] is True
    fresh=WordPlugin(store).prepare_creation(task_id='task',path='report.docx',
        blocks=[{'kind':'paragraph','text':'New'}])
    assert fresh['ref']==revised['ref'] and 'edit_effect' not in fresh
    before=snapshot(store)
    function=store.db.execute('SELECT function FROM native_calls WHERE call_id=?',(call,)).fetchone()[0]
    recovered=reconcile_word_call(store,'task')
    assert recovered['state']=='reconciled' and recovered['call_id']==call
    assert snapshot(store)==before
    row=store.db.execute('SELECT function,result_message FROM native_calls WHERE call_id=?',(call,)).fetchone()
    assert row['function']==function
    recorded=json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(row['result_message'],)).fetchone()[0])
    assert recorded==revised
    assert json.loads(exchanges(store,'task')[0][1]['content'])==revised
    assert 'edit_effect' not in repair.pending(store,'task')
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    assert store.candidate('report.docx') is None
