import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore,canonical
from localoffice.word.guidance import load,selected
from localoffice.errors import HarnessError
from test_word_tables import saved_table
from test_word_progress import task,change


class PressureProvider:
    context_limit=4096
    def status(self):return {'loaded':True,'constrained_decoding':False}
    async def count_tokens(self,messages):
        return 3500 if '"operations":["format_table"]' in messages[0]['content'] else 1000


@pytest.mark.asyncio
async def test_recycling_keeps_objective_ledger_and_evidence_and_survives_restart(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    try:
        objective='Center the table. Then review it; do not repeat the completed edit. Δ stays.'
        store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(objective,'task'))
        from localoffice.annotations import annotate,bind_annotations
        table=plugin.read_objects(path='report.docx',revision=rev,ids=['table:1'])['objects'][0]
        annotation=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':table['id'],'object_hash':table['hash']},comment='Only this table')
        bind_annotations(store,'task','report.docx',[annotation])
        load(store,'task',['format_table'])
        result=change(plugin,rev,'first',{'alignment':'center'})
        store.message('assistant','{"tool":"word.stage_batch","evidence":"latest action"}','task')
        store.message('tool',canonical(result),'task')
        runner=TaskRunner(store,PressureProvider());messages=await runner.assemble('task')
        system=messages[0]['content']
        state=json.loads(system.split('Current authoritative state: ',1)[1].split('\nActive Word operations:',1)[0])
        assert state['objective']==objective
        assert json.loads(state['edit_scope']['ranges'])=={'format':'docx','objects':['table:1']}
        assert state['annotations'][0]['comment']=='Only this table'
        assert state['target']['expected_revision']==result['candidate_revision']
        assert state['completed_word_changes']['receipts'][0]['receipt_id']==result['receipt_id']
        assert 'latest action' in messages[-2]['content'] and result['receipt_id'] in messages[-1]['content']
        assert runner.word_tool_state['operations']==[] and selected(store,'task')['operations']==[]
        assert runner.context_info['evicted_word_operations']==['format_table']
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==1
        assert store.db.execute("SELECT count(*) FROM operations WHERE task_id='task'").fetchone()[0]==1
        assert (tmp_path/'report.docx').read_bytes()==original
        store.close();store=ProjectStore(tmp_path)
        assert selected(store,'task')['operations']==[]
        await TaskRunner(store,PressureProvider()).assemble('task')
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==1
        store.db.execute("UPDATE tasks SET status='running' WHERE id='task'")
        load(store,'task',['format_cell']);assert selected(store,'task')['operations']==['format_cell']
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['no_execution','still_too_large','admission'])
async def test_recycling_does_not_hide_failed_admission_or_drop_mandatory_state(tmp_path,scenario):
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    class AlwaysLarge(PressureProvider):
        async def count_tokens(self,messages):return 3500
    try:
        load(store,'task',['format_table']);prior=selected(store,'task')
        if scenario!='no_execution':change(plugin,rev,'first',{'alignment':'center'})
        runner=TaskRunner(store,AlwaysLarge() if scenario=='still_too_large' else PressureProvider())
        kwargs={'word_tool_override':prior,'pending_tool_result':{'loaded_operations':['format_table']}} if scenario=='admission' else {}
        with pytest.raises(HarnessError) as error:await runner.assemble('task',**kwargs)
        assert error.value.code=='context_capacity'
        assert selected(store,'task')==prior
        assert store.db.execute("SELECT count(*) FROM task_measurements WHERE kind='context_recycle'").fetchone()[0]==0
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('operations',[['format_table'],['format_table','format_cell']])
async def test_failed_load_explains_single_vs_multiple_operation_pressure(tmp_path,operations):
    store,plugin,original,rev=saved_table(tmp_path)
    class RejectSelected(PressureProvider):
        async def count_tokens(self,messages):
            active=json.loads(messages[0]['content'].split('Active Word operations: ',1)[1].split('\n',1)[0])
            return 3500 if active.get('operations')==operations else 500
        async def stream(self,*args,**kwargs):
            yield {'type':'token','text':json.dumps({'tool':'word.load_tools','operations':operations})}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,RejectSelected())
    try:
        key=await runner.start('Review table formatting.',artifact='report.docx',mode='background');await runner.job
        errors=[e for e in runner.events if e['type']=='tool_error']
        assert len(errors)==2 and all(e['code']=='context_capacity' for e in errors)
        expected='This single operation does not fit' if len(operations)==1 else 'Load exactly one requested operation next'
        assert all(expected in e['message'] and 'prior selection is unchanged' in e['message'] for e in errors)
        assert selected(store,key)['operations']==[]
        assert not store.candidate('report.docx') and (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()
