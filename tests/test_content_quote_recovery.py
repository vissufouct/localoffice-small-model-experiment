"""Absent-quote recovery remains advisory, bounded and atomic."""
import copy
import json
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import canonical,digest
from localoffice.word.content_plan import plan,ACTIVE_PLAN_PREFIX
from test_word_content_plan import fixture


def failure(store,literals,**kwargs):
    with pytest.raises(HarnessError) as caught:plan(store,'task',literals=literals,**kwargs)
    return caught.value


def state(store):
    return {'refs':[tuple(row) for row in store.db.execute('SELECT * FROM word_content_refs ORDER BY ref')],
        'active':[tuple(row) for row in store.db.execute('SELECT * FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',))],
        'nodes':store.db.execute('SELECT count(*) FROM nodes').fetchone()[0],
        'edges':store.db.execute('SELECT count(*) FROM edges').fetchone()[0]}


def test_drafted_quote_failure_identifies_only_invalid_selection_and_preserves_valid_indices(tmp_path):
    objective='Use "Relatório de revisão", "Página ", " de ", "Síntese — café e inovação." and "Conclusão — próximos passos.". Draft a neutral English invitation to review.'
    store=fixture(tmp_path,objective)
    selections=[{'role':role,'quote':quote} for role,quote in [
        ('header','Relatório de revisão'),('footer','Página '),('footer',' de '),
        ('paragraph','Síntese — café e inovação.'),
        ('paragraph','This document is intended for your review and consideration.'),
        ('paragraph','Conclusão — próximos passos.')]]
    before=copy.deepcopy(selections)
    try:
        error=failure(store,selections);recovery=error.recovery
        assert error.code=='content_quote'
        assert recovery['validated_indices']==[0,1,2,3,5]
        assert recovery['validation_scope']=='quote_and_occurrence'
        assert recovery['source_hash']==digest(objective.encode())
        assert recovery['invalid']==[{'index':4,'role':'paragraph','reason':'not_in_source',
            'quote_preview':selections[4]['quote'],'quote_truncated':False}]
        assert recovery['omitted_invalid_count']==0 and recovery['truncated'] is False
        assert 'requested drafting needs no source quote' in recovery['instruction']
        assert 'decide: requested drafting or quotation needing correction' in recovery['instruction']
        assert 'do not drop uncertain requested literals' in recovery['instruction']
        assert 'Do not reconfirm supplied text.' in recovery['instruction']
        assert 'candidate_call' not in recovery and 'bindings' not in recovery
        assert len(canonical(recovery).encode())<=1024
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        assert selections==before
    finally:store.close()


@pytest.mark.parametrize('mode',['extend','replace'])
def test_existing_active_plan_and_refs_unchanged_on_error(tmp_path,mode):
    store=fixture(tmp_path,'Keep Existing and Valid. Draft a sentence.')
    try:
        plan(store,'task',literals=[{'role':'paragraph','quote':'Existing'}])
        before=state(store)
        result=failure(store,[{'role':'header','quote':'Valid'},{'role':'paragraph','quote':'New invented sentence'}],plan_mode=mode)
        assert result.recovery['validated_indices']==[0]
        assert state(store)==before
    finally:store.close()


@pytest.mark.parametrize('occurrence',[99,None])
def test_occurrence_only_errors_do_not_get_drafting_fallback(tmp_path,occurrence):
    store=fixture(tmp_path,'Repeated '*40)
    try:
        error=failure(store,[{'role':'paragraph','quote':'Repeated','occurrence':occurrence}])
        assert error.recovery is None
    finally:store.close()


def test_mixed_absent_and_occurrence_diagnostics_are_distinct(tmp_path):
    store=fixture(tmp_path,'Repeated '*40+'Unique')
    try:
        error=failure(store,[{'role':'header','quote':'Unique','occurrence':1},
            {'role':'paragraph','quote':'Unwritten generated sentence'},
            {'role':'paragraph','quote':'Repeated'}])
        reasons={item['index']:item['reason'] for item in error.recovery['invalid']}
        assert reasons=={0:'occurrence_out_of_range',1:'not_in_source',2:'occurrence_required'}
        assert error.recovery['validated_indices']==[]
        assert 'For not_in_source only, decide:' in error.recovery['instruction']
        assert 'Fix occurrence errors separately.' in error.recovery['instruction']
    finally:store.close()


def test_existing_prefix_candidate_recovery_is_preserved(tmp_path):
    store=fixture(tmp_path,'Use Page followed by native page fields. Keep Body.')
    try:
        error=failure(store,[{'role':'paragraph','quote':'Body'},{'role':'footer','quote':'Page of'}])
        assert error.recovery['candidate_only'] is True
        assert error.recovery['candidate_call']['literals'][1]['quote']=='Page '
        assert error.recovery.get('kind')!='selection_review'
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


def test_many_unicode_failures_have_explicit_omissions_and_safe_preview_bounds(tmp_path):
    store=fixture(tmp_path,'Only source')
    selections=[{'role':'paragraph','quote':'😀'*81+str(i)} for i in range(24)]
    try:
        result=failure(store,selections).recovery
        assert len(canonical(result).encode())<=1024
        assert result['truncated'] is True and result['omitted_invalid_count']>0
        assert len(result['invalid'])+result['omitted_invalid_count']==24
        assert len({item['index'] for item in result['invalid']})==len(result['invalid'])
        for item in result['invalid']:
            assert len(item['quote_preview'])<=80 and item['quote_truncated'] is True
            assert selections[item['index']]['quote'].startswith(item['quote_preview'])
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


def test_valid_index_list_is_never_truncated_to_fit_preview(tmp_path):
    store=fixture(tmp_path,' | '.join(f'Valid{i}!' for i in range(23)))
    try:
        choices=[{'role':'paragraph','quote':f'Valid{i}!'} for i in range(23)]+[{'role':'paragraph','quote':'界'*81}]
        recovery=failure(store,choices).recovery
        assert recovery['validated_indices']==list(range(23))
        assert len(canonical(recovery).encode())<=1024
        assert len(recovery['invalid'])+recovery['omitted_invalid_count']==1
    finally:store.close()


def test_ambiguous_common_prefix_keeps_drafting_scope_diagnostic(tmp_path):
    store=fixture(tmp_path,'The header is Literal. The next paragraph should be drafted.')
    try:
        before=state(store)
        error=failure(store,[{'role':'header','quote':'Literal'},
                             {'role':'paragraph','quote':'The generated sentence is not a quotation.'}])
        recovery=error.recovery
        assert recovery['kind']=='selection_review'
        assert recovery['validated_indices']==[0]
        assert recovery['invalid'][0]['index']==1
        assert 'requested drafting needs no source quote' in recovery['instruction']
        assert 'candidate_call' not in recovery
        assert state(store)==before
    finally:store.close()
