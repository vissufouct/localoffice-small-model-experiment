import json
import pytest
from localoffice.storage import ProjectStore, canonical
from localoffice.runtime import TaskRunner
from test_word_progress import task


@pytest.mark.asyncio
@pytest.mark.parametrize('restart',[False,True])
async def test_exact_creation_objective_survives_window_pruning_and_repair(tmp_path,restart):
    class Provider:
        context_limit=4096
        def status(self):return {'constrained_decoding':False}
        async def count_tokens(self,messages):return sum(len(m['content'])//4 for m in messages)
    store=ProjectStore(tmp_path);task(store)
    objective='Create report.docx with exact text “Primeira página”.\nKeep “Δ = 2” unchanged. Footer: Page 1 of 2 as fields.'
    store.db.execute('UPDATE tasks SET objective=? WHERE id=?',(objective,'task'))
    store.message('user',objective,'task')
    for i in range(12):
        store.message('assistant',canonical({'tool':'word.create','invalid_attempt':i,'filler':'x'*1400}),'task')
        store.message('tool',canonical({'error':'invalid_schema','message':'Retry the invalid shape','filler':'y'*1400}),'task')
    store.message('assistant','{"tool":"word.create","invalid_shape":true}','task')
    store.message('tool','{"error":"invalid_schema","message":"Keep original content; correct shape"}','task')
    if restart:store.close();store=ProjectStore(tmp_path)
    try:
        runner=TaskRunner(store,Provider());messages=await runner.assemble('task')
        assert runner.context_info['omitted_messages']>0
        state=messages[0]['content'].split('Current authoritative state: ',1)[1].split('\nActive Word operations:',1)[0]
        assert json.loads(state)['objective']==objective
        assert 'invalid_shape' in messages[-2]['content'] and 'correct shape' in messages[-1]['content']
        assert store.db.execute("SELECT content FROM messages WHERE role='user' ORDER BY id LIMIT 1").fetchone()[0]==objective
        assert store.candidate('report.docx') is None
    finally:store.close()
