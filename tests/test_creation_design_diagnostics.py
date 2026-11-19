"""Page-break diagnostics identify current structure, never inferred pagination."""
import copy
import pytest
from localoffice.storage import canonical
from localoffice.word import creation_repair as repair
from test_word_content_plan import fixture


@pytest.mark.parametrize('kinds,expected',[
    (['page_break','paragraph'],{'blocks.0':['leading']}),
    (['paragraph','page_break'],{'blocks.1':['trailing']}),
    (['page_break'],{'blocks.0':['leading','trailing']}),
    (['paragraph','page_break','page_break','paragraph'],
     {'blocks.1':['adjacent'],'blocks.2':['adjacent']}),
])
def test_exact_positions_and_reasons_preserve_original(tmp_path,kinds,expected):
    store=fixture(tmp_path)
    payload={'path':'report.docx','blocks':[{'kind':k,**({'text':'Text'} if k=='paragraph' else {})} for k in kinds]}
    original=copy.deepcopy(payload)
    try:
        result=repair.capture(store,'task',payload,allow_valid=True)
        diagnostics=result['validation_errors']
        assert {e['path']:e['reasons'] for e in diagnostics['items']}==expected
        assert all(e['kind']=='page_break' for e in diagnostics['items'])
        assert diagnostics['omitted_count']==0
        assert diagnostics['repair_tool']=='word.revise_creation'
        assert len(canonical(diagnostics).encode())<=640
        assert payload==original==repair.get(store,'task')['payload']
        assert 'destination' not in canonical(diagnostics) and 'move' not in canonical(diagnostics)
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def test_many_violations_bounded_and_explicitly_omitted(tmp_path):
    store=fixture(tmp_path)
    try:
        payload={'path':'report.docx','blocks':[{'kind':'page_break'} for _ in range(24)]}
        result=repair.capture(store,'task',payload,allow_valid=True)
        diag=result['validation_errors']
        assert len(canonical(diag).encode())<=640
        assert len(diag['items'])+diag['omitted_count']==24
        assert diag['omitted_count']>0
        assert len(repair.get(store,'task')['payload']['blocks'])==24
        assert repair.pending(store,'task')['validation_errors']==diag
    finally:store.close()


def test_unmapped_root_error_preserved_with_exact_break_facts(tmp_path):
    store=fixture(tmp_path)
    try:
        payload={'path':'report.docx','styles':[{'id':'Same'},{'id':'Same'}],
                 'blocks':[{'kind':'paragraph','text':'Text'},{'kind':'page_break'}]}
        result=repair.capture(store,'task',payload,allow_valid=True)
        diag=result['validation_errors']
        assert diag['items'][0]['path']=='blocks.1'
        assert any(e['path']=='$' and 'unique' in e['message'] for e in diag['items'])
        assert diag['omitted_count']==0
    finally:store.close()


def test_valid_break_not_reported_for_other_design_error(tmp_path):
    store=fixture(tmp_path)
    try:
        payload={'path':'report.docx','styles':[{'id':'Same'},{'id':'Same'}],
                 'blocks':[{'kind':'paragraph','text':'A'},{'kind':'page_break'},
                           {'kind':'paragraph','text':'B'}]}
        diag=repair.capture(store,'task',payload,allow_valid=True)['validation_errors']
        assert len(diag['items'])==1 and diag['items'][0]['path']=='$'
        assert 'reasons' not in diag['items'][0]
    finally:store.close()
