"""Canonical native creation source shape; host legacy compatibility retained."""
import copy
import pytest
from localoffice.native_tools import action_tools,compact_tools,validate_native_arguments
from localoffice.word.guidance import project_schema
from localoffice.runtime import WORD_ACTION
from localoffice.word.contracts import CreateDocument
from localoffice.errors import HarnessError

@pytest.mark.parametrize('operation',['create','prepare_creation'])
def test_inline_refs_cover_all_creation_slots_without_legacy_map(operation):
    full=project_schema(WORD_ACTION.json_schema(),[operation]);before=copy.deepcopy(full)
    tools=compact_tools(action_tools(full));name='word.'+operation
    args={'path':'report.docx','revision':None,'blocks':[
        {'kind':'paragraph','text':{'ref':'ct_'+'a'*24}},
        {'kind':'paragraph','text':'Requested original drafting'},
        {'kind':'table','rows':[[{'ref':'ct_'+'b'*24},'Draft cell']]}]}
    for band in ('header','footer','first_header','first_footer','even_header','even_footer'):
        args[band]={'text':{'ref':'ct_'+'c'*24}}
    validate_native_arguments(name,args,tools)
    assert full==before
    params=next(t['function']['parameters'] for t in tools if t['function']['name']==name)
    assert 'literal_refs' not in params['properties']
    with pytest.raises(HarnessError) as exc:
        validate_native_arguments(name,{**args,'literal_refs':{}},tools)
    assert exc.value.code=='native_arguments'


def test_host_legacy_map_and_explicit_finalization_bindings_remain_available():
    legacy={'path':'report.docx','blocks':[{'kind':'paragraph','text':''}],
            'literal_refs':{'blocks.0.text':'ct_'+'a'*24}}
    assert CreateDocument.model_validate(legacy).literal_refs==legacy['literal_refs']
    tools=action_tools(project_schema(WORD_ACTION.json_schema(),['finalize_creation']))
    validate_native_arguments('word.finalize_creation',{'ref':'cr_'+'a'*32,'styles':[],
        'bindings':[{'slot':'blocks.0.text','ref':'ct_'+'a'*24}]},tools)


@pytest.mark.parametrize('linked_call',[False,True])
def test_legacy_map_recovery_requires_exact_call_without_new_execution(tmp_path,monkeypatch,linked_call):
    from localoffice.storage import ProjectStore
    from localoffice.word.plugin import WordPlugin
    from localoffice.word.content_plan import plan
    from localoffice.native_history import record_call,reconcile_word_call
    from test_word_progress import task
    store=ProjectStore(tmp_path);task(store)
    try:
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Exact body',artifact='report.docx' WHERE id='task'")
        ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Exact body'}])['literals'][0]['ref']
        old={'tool':'word.prepare_creation','path':'report.docx','blocks':[{'kind':'paragraph','text':''}],
             'literal_refs':{'blocks.0.text':ref}}
        call=record_call(store,'task',old)
        WordPlugin(store).prepare_creation(task_id='task',native_call_id=call if linked_call else None,
            **{k:v for k,v in old.items() if k!='tool'})
        def forbidden(*args,**kwargs):raise AssertionError('No recovery mutation allowed')
        monkeypatch.setattr(WordPlugin,'_execute',forbidden)
        monkeypatch.setattr('localoffice.word.creation_repair.capture',forbidden)
        assert reconcile_word_call(store,'task')['state']==('reconciled' if linked_call else 'unresolved')
        assert not store.candidate('report.docx')
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()
