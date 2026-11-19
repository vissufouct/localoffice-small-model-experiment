"""Complete selected plan plus exact-pair historical summary; no fabricated calls."""
import copy
import json
import pytest
from localoffice.storage import canonical,digest
from localoffice.errors import HarnessError
from localoffice.native_history import record_call,record_result,project_message
from localoffice.native_tools import validate_history
from localoffice.word.requirements import plan,RESULT_PREFIX
from localoffice.word.requirement_plan_context import snapshot,project_pair,STATE_KEY
from localoffice.word.objective_evidence import catalogue
from localoffice.chats import create_chat,select_chat
from test_word_content_plan import fixture

OBJECTIVE='Use one-inch margins. Draft 8–16 words.'


def setup(store,empty=False,ranges=False):
    entries=[] if empty else [
        {'kind':'page_geometry','source_quote':'one-inch margins','field':'margin','equals':1.0,'unit':'in'},
        {'kind':'paragraph_words','source_quote':'8–16 words','paragraph_index':1,'min':8,'max':16},
        {'kind':'paragraph_words','source_quote':'8–16 words','paragraph_index':0,'min':0,'max':20}]
    if ranges:
        c=catalogue(store,'task')
        for entry in entries:
            quote=entry.pop('source_quote')
            name=next(k for k,v in c['segments'].items() if quote in v['text'])
            entry['source_range']={'start':name,'end':name}
    args={'source_hash':digest(OBJECTIVE.encode()),'requirements':entries}
    call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
    receipt=plan(store,'task',**args,native_call_id=call)
    message_id=record_result(store,'task',call,receipt)
    row=store.db.execute('SELECT * FROM native_calls WHERE call_id=?',(call,)).fetchone()
    a=dict(store.db.execute('SELECT * FROM messages WHERE id=?',(row['assistant_message'],)).fetchone())
    t=dict(store.db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone())
    return args,call,project_message(store,a,a['content']),project_message(store,t,'TOOL RECEIPT / SOURCE EVIDENCE: '+t['content'])


@pytest.mark.parametrize('empty,ranges',[(False,False),(True,False),(False,True)])
def test_complete_snapshot_and_verified_pair_collapse_are_read_only(tmp_path,empty,ranges):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        args,call,a,t=setup(store,empty,ranges)
        before='\n'.join(store.db.iterdump());original=copy.deepcopy((a,t))
        visible=snapshot(store,'task')
        assert visible['complete'] is True and visible['coverage']=='unassessed'
        assert len(visible['requirements'])==len(args['requirements'])
        assert len(canonical(visible).encode())<=2048
        for expected,actual in zip(args['requirements'],visible['requirements']):
            assert {k:v for k,v in actual.items() if k!='source_span'}=={k:v for k,v in expected.items() if k not in ('source_quote','source_range')}
            start,end=visible['source_spans'][actual['source_span']]
            assert OBJECTIVE[start:end]
        if not empty:
            assert len(visible['source_spans'])==2
            # Preserve model-invented bounds without certifying their meaning.
            assert visible['requirements'][2]['min']==0 and visible['requirements'][2]['max']==20
        summary=project_pair(store,'task',call,a,t,visible)
        assert set(summary)=={'role','content'} and summary['role']=='assistant'
        payload=json.loads(summary['content'])['historical_tool_summary']
        assert payload['call_id']==call and payload['generation']==visible['generation']
        assert payload['details_in_current_state']==STATE_KEY
        assert payload['document_changed'] is False
        validate_history([{'role':'user','content':OBJECTIVE},summary])
        assert (a,t)==original and '\n'.join(store.db.iterdump())==before
    finally:store.close()


@pytest.mark.parametrize('case',['missing_state','truncated','missing_entry','changed_value','wrong_span',
    'assistant_args','assistant_id','tool_id','tool_receipt','unexecuted','superseded','stale'])
def test_projection_abstains_without_exact_complete_current_provenance(tmp_path,case):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        args,call,a,t=setup(store);visible=snapshot(store,'task')
        if case=='missing_state':visible=None
        elif case=='truncated':visible['complete']=False
        elif case=='missing_entry':visible['requirements'].pop()
        elif case=='changed_value':visible['requirements'][0]['equals']=2
        elif case=='wrong_span':visible['source_spans'][0][0]+=1
        elif case=='assistant_args':a['tool_calls'][0]['function']['arguments']['requirements']=[]
        elif case=='assistant_id':a['tool_calls'][0]['id']='call_'+'f'*32
        elif case=='tool_id':t['tool_call_id']='call_'+'f'*32
        elif case=='tool_receipt':t['content']+=' '
        elif case=='unexecuted':call=record_call(store,'task',{'tool':'word.plan_requirements',**args})
        elif case=='superseded':plan(store,'task',**args);visible=snapshot(store,'task')
        elif case=='stale':store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        before='\n'.join(store.db.iterdump())
        assert project_pair(store,'task',call,a,t,visible) is None
        assert '\n'.join(store.db.iterdump())==before
    finally:store.close()


def test_missing_stale_small_budget_and_corruption_are_not_empty_plans(tmp_path):
    store=fixture(tmp_path,OBJECTIVE)
    try:
        assert snapshot(store,'task') is None
        _,call,a,t=setup(store)
        assert snapshot(store,'task',max_bytes=10) is None
        for budget in [0,2049,True]:
            with pytest.raises(ValueError):snapshot(store,'task',max_bytes=budget)
        visible=snapshot(store,'task')
        key=RESULT_PREFIX+call
        env=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0]);env['hash']='0'*64
        store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(env),key))
        with pytest.raises(HarnessError,match='integrity'):project_pair(store,'task',call,a,t,visible)
        select_chat(store,create_chat(store))
        with pytest.raises(HarnessError):snapshot(store,'task')
    finally:store.close()
