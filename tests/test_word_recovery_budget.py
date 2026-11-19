import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.metrics import summary
from test_word_tables import saved_table
from test_word_text_format import get


class Scripted:
    context_limit=16384
    def __init__(self,actions):self.actions=iter(actions);self.calls=0
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 500
    async def stream(self,messages,**kwargs):
        self.calls+=1
        yield {'type':'token','text':json.dumps(next(self.actions))}
        yield {'type':'usage','usage':{'prompt_tokens':500,'completion_tokens':50}}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.asyncio
@pytest.mark.parametrize('last',['bad','finish'])
async def test_loading_guidance_does_not_erase_failure_or_retry_budget(tmp_path,last):
    store,plugin,original,rev=saved_table(tmp_path)
    bad={'tool':'word.create_character_style','path':'report.docx','revision':rev}
    load={'tool':'word.load_tools','operations':['create_character_style']}
    provider=Scripted([bad,load,bad if last=='bad' else {'tool':'finish','text':'Done'}])
    runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Create the requested character style',artifact='report.docx',mode='background');await runner.job
        assert provider.calls==3
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='failed'
        assert any(e['type']=='error' and e['code']=='retry_limit' for e in runner.events)
        if last=='finish':assert any(e.get('code')=='unresolved_tool_error' for e in runner.events)
        assert not any(e['type']=='completed' for e in runner.events)
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
        metrics=summary(store,task)
        assert metrics['model_calls']==3 and metrics['repair_events']==2
        assert metrics['input_tokens']==1500 and metrics['output_tokens']==150
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_loading_then_successful_correction_allows_completion(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);target=get(plugin,rev)
    bad={'tool':'word.format_text','path':'report.docx','revision':rev,'target':target['id'],'expected_hash':'0'*64,'start':0,'end':3,'properties':{'bold':True}}
    good={**bad,'expected_hash':target['hash']}
    provider=Scripted([bad,{'tool':'word.load_tools','operations':['format_text']},good,{'tool':'finish','text':'Ready for review'}])
    runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Bold the first three letters of the introduction',artifact='report.docx',mode='background');await runner.job
        assert provider.calls==4
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        assert store.candidate('report.docx') is not None
        assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
        assert (tmp_path/'report.docx').read_bytes()==original
        assert summary(store,task)['repair_events']==1
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('last',['finish','bad','correct'])
async def test_reading_evidence_does_not_clear_rejected_mutation(tmp_path,last):
    store,plugin,original,rev=saved_table(tmp_path);target=get(plugin,rev)
    bad={'tool':'word.format_text','path':'report.docx','revision':rev,'target':target['id'],'expected_hash':'0'*64,'start':0,'end':3,'properties':{'bold':True}}
    read={'tool':'word.read_objects','path':'report.docx','revision':rev,'ids':[target['id']]}
    finish={'tool':'finish','text':'Done'}
    ending=[{**bad,'expected_hash':target['hash']},finish] if last=='correct' else [finish if last=='finish' else bad]
    provider=Scripted([bad,read,*ending]);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Bold the requested span',artifact='report.docx',mode='background');await runner.job
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        assert state==('completed' if last=='correct' else 'failed')
        assert provider.calls==(4 if last=='correct' else 3)
        assert bool(store.candidate('report.docx'))==(last=='correct')
        assert (tmp_path/'report.docx').read_bytes()==original
        if last!='correct':assert any(e['type']=='error' and e['code']=='retry_limit' for e in runner.events)
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_corrected_read_only_failure_can_finish_without_mutation(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    read={'tool':'word.read_objects','path':'report.docx','revision':rev,'ids':['paragraph:2']}
    provider=Scripted([{**read,'ids':['paragraph:999']},read,{'tool':'finish','text':'Read complete'}]);runner=TaskRunner(store,provider)
    try:
        task=await runner.start('Read the introduction only',artifact='report.docx',mode='background');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
        assert summary(store,task)['repair_events']==1
    finally:await runner.pause();store.close()
