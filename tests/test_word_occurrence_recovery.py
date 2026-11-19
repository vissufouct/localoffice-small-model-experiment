"""Advisory single-match occurrence repair never changes source selections."""
import copy
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import canonical, digest
from localoffice.word.content_plan import plan, active_plan
from test_word_content_plan import fixture


def snapshot(store):
    return {table:[tuple(row) for row in store.db.execute('SELECT * FROM '+table)]
            for table in ('meta','word_content_refs','nodes','edges','operations')}


def rejected(store,request):
    with pytest.raises(HarnessError) as error:plan(store,'task',**request)
    return error.value


def test_all_unique_occurrence_errors_advisory_then_explicit_resubmission(tmp_path):
    source='Body one | First header | Even header | Default footer'
    store=fixture(tmp_path,source)
    try:
        request={'literals':[{'role':'paragraph','quote':'Body one','occurrence':None},
                             {'role':'header','quote':'First header','occurrence':1},
                             {'role':'header','quote':'Even header','occurrence':2},
                             {'role':'footer','quote':'Default footer','occurrence':3}]}
        original=copy.deepcopy(request);before=snapshot(store)
        error=rejected(store,request);recovery=error.recovery
        assert error.code=='content_quote' and recovery['candidate_only'] is True
        assert recovery['source_hash']==digest(source.encode())
        expected={**copy.deepcopy(request),'tool':'word.plan_content'}
        for item in expected['literals'][1:]:item['occurrence']=0
        assert recovery['candidate_call']==expected
        assert request==original and snapshot(store)==before
        assert len(canonical(recovery).encode())<=2048
        result=plan(store,'task',**recovery['candidate_call'])
        assert [x['text'] for x in result['literals']]==[x['quote'] for x in original['literals']]
        assert active_plan(store,'task')['refs']==[{'ref':x['ref'],'role':x['role']} for x in result['literals']]
        assert result['document_changed'] is False
    finally:store.close()


@pytest.mark.parametrize('case',['multiple_matches','mixed_absent','many_matches'])
def test_ambiguous_or_mixed_failures_never_get_occurrence_candidate(tmp_path,case):
    source='Unique | Repeat | Repeat | '+('Many '*33)
    store=fixture(tmp_path,source)
    try:
        selections=[{'role':'header','quote':'Unique','occurrence':1}]
        selections.append({'role':'footer','quote':{'multiple_matches':'Repeat','mixed_absent':'Absent','many_matches':'Many'}[case],
                           **({'occurrence':4} if case=='multiple_matches' else {})})
        before=snapshot(store);error=rejected(store,{'literals':selections})
        assert not error.recovery or 'candidate_call' not in error.recovery
        assert snapshot(store)==before
    finally:store.close()


def test_replace_source_message_and_valid_explicit_occurrence_preserved(tmp_path):
    store=fixture(tmp_path,'Original selected body')
    try:
        historical=plan(store,'task',literals=[{'role':'paragraph','quote':'Original selected body'}])['literals'][0]['ref']
        message=store.message('user','Repeat | Repeat | First header | Even footer','task')
        request={'tool':'word.plan_content','source_message':message,'plan_mode':'replace','literals':[
            {'role':'paragraph','quote':'Repeat','occurrence':1},
            {'role':'header','quote':'First header','occurrence':3},
            {'role':'footer','quote':'Even footer','occurrence':4}]}
        before=snapshot(store);error=rejected(store,request)
        expected=copy.deepcopy(request)
        for item in expected['literals'][1:]:item['occurrence']=0
        assert error.recovery['candidate_call']==expected
        assert snapshot(store)==before
        result=plan(store,'task',**error.recovery['candidate_call'])
        assert all(item['source']=='message:'+str(message) for item in result['literals'])
        assert result['literals'][0]['start']==9
        assert historical not in [x['ref'] for x in active_plan(store,'task')['refs']]
        assert store.db.execute('SELECT 1 FROM word_content_refs WHERE ref=?',(historical,)).fetchone()
    finally:store.close()


@pytest.mark.parametrize('use_message',[False,True])
def test_changed_source_is_revalidated_on_explicit_candidate_execution(tmp_path,use_message):
    store=fixture(tmp_path,'Unique header')
    try:
        request={'plan_mode':'replace','literals':[{'role':'header','quote':'Unique header','occurrence':5}]}
        if use_message:request['source_message']=store.message('user','Unique header','task')
        candidate=rejected(store,request).recovery['candidate_call']
        with store.transaction():
            if use_message:store.db.execute('UPDATE messages SET content=? WHERE id=?',('Changed text',request['source_message']))
            else:store.db.execute("UPDATE tasks SET objective='Changed text' WHERE id='task'")
        before=snapshot(store)
        error=rejected(store,candidate)
        assert error.code=='content_quote'
        assert snapshot(store)==before
        assert not store.db.execute('SELECT 1 FROM word_content_refs').fetchone()
    finally:store.close()


def test_oversized_candidate_abstains_without_truncating_literals(tmp_path):
    quote='界'*700
    store=fixture(tmp_path,quote)
    try:
        before=snapshot(store)
        error=rejected(store,{'literals':[{'role':'paragraph','quote':quote,'occurrence':9}]})
        assert error.recovery is None or 'candidate_call' not in error.recovery
        assert snapshot(store)==before
    finally:store.close()
