import asyncio
import json
import time
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import ProjectStore
from localoffice.metrics import summary
from localoffice.chats import create_chat,select_chat
from localoffice.errors import HarnessError
from test_word_worker import fixture_docx


@pytest.mark.asyncio
@pytest.mark.parametrize("filename",["source.docx","source.DOCX"])
async def test_word_task_scoped_schema_real_worker_and_metrics(tmp_path,filename):
    source=tmp_path/filename;fixture_docx(source);original=source.read_bytes()
    store=ProjectStore(tmp_path);revision=store.file_hash(filename)
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 700
        async def stream(self,messages,**kwargs):
            schema=json.dumps(kwargs['schema'])
            assert ('word.stage_batch' in schema)==(self.step>0) and 'stage_markdown' not in schema
            assert 'FormatTable' not in kwargs['schema'].get('$defs',{})
            assert 'Compact Word workflow marker' in messages[0]['content']
            self.step+=1
            if self.step==1:action={'tool':'word.load_tools','operations':['replace_text']}
            elif self.step==2:action={'tool':'word.list_objects','path':filename,'revision':revision,'limit':1}
            elif self.step==3:
                result=json.loads(store.db.execute("SELECT content FROM messages WHERE role='tool' ORDER BY id DESC LIMIT 1").fetchone()[0])
                obj=result['objects'][0]
                action={'tool':'word.stage_batch','path':filename,'revision':revision,'operations':[
                    {'id':'e1','op':'replace_text','target':obj['id'],'expected_hash':obj['hash'],'start':1,'end':6,'text':'New'}]}
            else:action={'tool':'finish','text':'Working copy ready for review.'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'usage','usage':{'prompt_tokens':700,'completion_tokens':50}}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    skill=runner.skills.create_draft('compact-word','Edit Word precisely','Compact Word workflow marker',['word.list_objects','word.stage_batch'])
    try:
        task=await runner.start('Replace the selected text',artifact=filename,skill_id=skill['id']);await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        assert store.candidate(filename) and source.read_bytes()==original
        measurements=summary(store,task)
        assert measurements['model_calls']==4 and measurements['tool_calls']==3
        assert measurements['input_tokens']==2800 and measurements['output_tokens']==200
        assert measurements['usage_complete']
        assert store.db.execute("SELECT 1 FROM edges WHERE source=? AND relation='read-object'",('task:'+task,)).fetchone()
        select_chat(store,create_chat(store,'Other'))
        with pytest.raises(HarnessError):summary(store,task)
    finally:store.close()


@pytest.mark.asyncio
async def test_pause_settles_word_thread_before_return(tmp_path,monkeypatch):
    import threading
    from localoffice.word.plugin import WordPlugin
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    started=threading.Event();settled=threading.Event()
    def delayed(**kwargs):
        started.set();time.sleep(.08);settled.set();return {}
    monkeypatch.setattr(plugin,'list_objects',delayed)
    task=asyncio.create_task(plugin.call_async('list_objects'))
    while not started.is_set():await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert settled.is_set()
    store.close()


@pytest.mark.asyncio
async def test_usage_counts_repair_and_marks_missing_usage_unknown(tmp_path):
    store=ProjectStore(tmp_path)
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 100
        async def stream(self,messages,**kwargs):
            self.step+=1
            yield {'type':'token','text':'Not JSON' if self.step==1 else '{"tool":"finish","text":"Ready"}'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Say ready');await runner.job
        result=summary(store,task)
        assert result['model_calls']==2 and result['repair_events']==1
        assert result['input_tokens'] is None and result['output_tokens'] is None and not result['usage_complete']
        store.close();store=ProjectStore(tmp_path)
        assert summary(store,task)['model_calls']==2
    finally:store.close()
