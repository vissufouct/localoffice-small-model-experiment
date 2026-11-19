import copy
import json
import pytest
from localoffice.storage import ProjectStore,canonical
from localoffice.native_history import record_call,record_result
from localoffice.word.requirement_context import project
from test_word_progress import task


@pytest.mark.parametrize('change',['none','ref','facts','status','unrecorded','receipt','tool'])
def test_only_exact_current_native_failure_is_projected(tmp_path,change):
    store=ProjectStore(tmp_path);task(store)
    try:
        ref='cr_'+'a'*32
        checks={'status':'mismatch','coverage':'unassessed','generation':1,
                'items':[{'actual':6,'expected':{'min':8,'max':16}}],'omitted_count':0}
        receipt={'code':'requirements_mismatch','message':'Correct selected constraints.',
                 'recovery':checks,'other_evidence':'preserved'}
        proposal={'ref':ref,'status':'prepared','selected_requirements':copy.deepcopy(checks)}
        call=record_call(store,'task',{'tool':'word.finalize_creation' if change!='tool' else 'word.revise_creation',
            'ref':ref,'styles':[]})
        message=record_result(store,'task',call,receipt)
        stored=store.db.execute('SELECT content FROM messages WHERE id=?',(message,)).fetchone()[0]
        if change=='ref':proposal['ref']='cr_'+'b'*32
        if change=='facts':proposal['selected_requirements']['items'][0]['actual']=7
        if change=='status':proposal['status']='stale'
        if change=='unrecorded':message=None
        if change=='receipt':receipt={**receipt,'message':'Different saved result'}
        before=copy.deepcopy(receipt)
        projected=project(store,'task',message,receipt,proposal)
        if change=='none':
            assert projected['recovery']=={'details_in_current_state':'creation_repair.selected_requirements'}
            assert {k:v for k,v in projected.items() if k!='recovery'}=={k:v for k,v in receipt.items() if k!='recovery'}
            assert len(canonical(projected))<len(canonical(receipt))
        else:assert projected==receipt
        assert receipt==before
        assert json.loads(stored)['recovery']==checks
        assert store.db.execute('SELECT content FROM messages WHERE id=?',
            (store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(call,)).fetchone()[0],)).fetchone()[0]==stored
    finally:store.close()
