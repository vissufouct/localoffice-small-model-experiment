import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.evidence import model_receipt,resolve
from localoffice.errors import HarnessError
from test_word_progress import task
from test_word_tables import saved_table
from test_word_recovery_budget import Scripted
from localoffice.runtime import TaskRunner


def read(plugin,rev,task_id='task'):
    return plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'],task_id=task_id)


def edit(plugin,rev,obj,key='edit',task_id='task',**changes):
    args={'path':'report.docx','revision':rev,'operations':[{'op':'format_text','target':obj['id'],'expected_hash':obj['evidence_ref'],'start':0,'end':3,'properties':{'bold':True}}]}
    args.update(changes)
    return plugin.stage_batch(**args,idempotency_key=key,task_id=task_id)


def test_issued_reference_is_stable_resolved_exactly_and_replayed_without_reread(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    try:
        receipt=read(plugin,rev);obj=receipt['objects'][0]
        assert len(obj['hash'])==64 and len(obj['evidence_ref'])==19
        assert read(plugin,rev)['objects'][0]['evidence_ref']==obj['evidence_ref']
        projected=model_receipt(receipt)
        assert projected['objects'][0]['hash']==obj['evidence_ref'] and 'evidence_ref' not in projected['objects'][0]
        assert receipt['objects'][0]['hash']==obj['hash']
        assert 'evidence_ref' not in read(plugin,rev,task_id=None)['objects'][0]
        result=edit(plugin,rev,obj)
        args=json.loads(store.db.execute('SELECT args FROM operations WHERE id=?',(result['receipt_id'],)).fetchone()[0])
        assert args['request']['operations'][0]['expected_hash']==obj['hash']
        assert args['submitted_request']['operations'][0]['expected_hash']==obj['evidence_ref']
        assert (tmp_path/'report.docx').read_bytes()==original
        with store.transaction():store.db.execute('DELETE FROM word_evidence')
        assert edit(plugin,rev,obj)==result
        with pytest.raises(HarnessError,match='different arguments'):
            edit(plugin,rev,{**obj,'evidence_ref':'ev_'+'0'*16})
        assert store.db.execute("SELECT count(*) FROM operations WHERE task_id='task'").fetchone()[0]==1
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('mismatch',['task','path','revision','object','unknown','missing_task'])
def test_reference_cannot_be_rebound(tmp_path,mismatch):
    store,plugin,original,rev=saved_table(tmp_path);task(store);task(store,'other')
    try:
        obj=read(plugin,rev)['objects'][0]
        values={'task_id':'task','path':'report.docx','revision':rev,'object_id':obj['id'],'ref':obj['evidence_ref']}
        field={'task':'task_id','path':'path','revision':'revision','object':'object_id','unknown':'ref','missing_task':'task_id'}[mismatch]
        values[field]={'task':'other','path':'other.docx','revision':'0'*64,'object':'paragraph:1','unknown':'ev_'+'0'*16,'missing_task':None}[mismatch]
        with pytest.raises(HarnessError):resolve(store,**values)
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_reference_survives_restart_but_not_revision_change_or_forget(tmp_path):
    from localoffice.memory import forget_project_memory
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    obj=read(plugin,rev)['objects'][0];store.close();store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        assert resolve(store,'task','report.docx',rev,obj['id'],obj['evidence_ref'])==obj['hash']
        result=edit(plugin,rev,obj)
        with pytest.raises(HarnessError,match='evidence reference'):edit(plugin,result['candidate_revision'],obj,key='stale-ref')
        assert store.candidate('report.docx')['candidate_hash']==result['candidate_revision']
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        assert store.db.execute('SELECT count(*) FROM word_evidence').fetchone()[0]==0
    finally:store.close()


@pytest.mark.asyncio
async def test_runner_projects_reference_but_retains_full_hash_in_graph_and_ledger(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    class Provider(Scripted):
        async def stream(self,messages,**kwargs):
            if self.calls==0:action={'tool':'word.load_tools','operations':['format_text']}
            elif self.calls==1:action={'tool':'word.read_objects','path':'report.docx','revision':rev,'ids':['paragraph:2']}
            elif self.calls==2:
                result=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1]);obj=result['objects'][0]
                assert obj['hash'].startswith('ev_') and 'evidence_ref' not in obj
                action={'tool':'word.format_text','path':'report.docx','revision':rev,'target':obj['id'],'expected_hash':obj['hash'],'start':0,'end':3,'properties':{'bold':True}}
            else:action={'tool':'finish','text':'Ready'}
            self.calls+=1
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider([]))
    try:
        key=await runner.start('Bold the first three letters in the introduction.',artifact='report.docx',mode='background');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed'
        row=store.db.execute('SELECT object_hash FROM word_evidence WHERE task_id=?',(key,)).fetchone()
        assert row and len(row[0])==64
        args=json.loads(store.db.execute('SELECT args FROM operations WHERE task_id=?',(key,)).fetchone()[0])
        assert args['request']['operations'][0]['expected_hash']==row[0]
        assert row[0] in store.db.execute("SELECT body FROM nodes WHERE id=?",('word:'+rev+':paragraph:2',)).fetchone()[0]
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()
