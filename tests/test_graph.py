import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.graph import neighborhood
from localoffice.chats import create_chat,select_chat
from localoffice.errors import HarnessError


def test_graph_is_bounded_cycle_safe_and_filters_private_decisions(tmp_path):
    store=ProjectStore(tmp_path)
    private=store.decision('Secret mango',store.message('user','Secret mango'))
    shared=store.decision('Project spelling',store.message('user','Project spelling'),scope='project')
    with store.transaction():
        store.node('file:report.md','file',{'path':'report.md'})
        store.edge('file:report.md','decision:'+private,'uses','test')
        store.edge('file:report.md','decision:'+shared,'uses','test')
        store.edge('decision:'+shared,'file:report.md','cycle','test')
    select_chat(store,create_chat(store,'Other'))
    result=neighborhood(store,'file:report.md')
    assert 'Secret mango' not in json.dumps(result)
    assert private not in json.dumps(result)
    assert 'Project spelling' in json.dumps(result)
    assert len(result['nodes'])==2
    assert neighborhood(store,'decision:'+private)['nodes']==[]
    with store.transaction():
        for i in range(80):
            store.node('revision:'+str(i),'revision',{'path':'report.md','long':'x'*4000})
            store.edge('file:report.md','revision:'+str(i),'contains','test')
    result=neighborhood(store,'file:report.md',max_nodes=4,max_edges=5,max_chars=2000)
    assert result['limited']
    assert len(result['nodes'])<=4 and len(result['edges'])<=5
    assert all(len(n['body_excerpt'])<=600 for n in result['nodes'])
    assert neighborhood(store,'file:missing.md')['nodes']==[]
    with pytest.raises(HarnessError):neighborhood(store,'file:report.md',depth=99)
    store.close()


def test_graph_receipts_follow_current_chat_ownership(tmp_path):
    store=ProjectStore(tmp_path)
    op=store.stage(path='draft.md',text='Draft',expected_revision=None,idempotency_key='write')
    receipt='receipt:'+op['id']
    assert neighborhood(store,receipt)['nodes']
    select_chat(store,create_chat(store,'Separate'))
    result=neighborhood(store,'file:draft.md')
    assert receipt not in [n['id'] for n in result['nodes']]
    assert neighborhood(store,receipt)['nodes']==[]
    store.close()

@pytest.mark.asyncio
async def test_real_tool_reads_link_exact_source_revisions_and_pages(tmp_path):
    """Synthetic action producer, real filesystem/PDF tools and persistent graph."""
    import shutil
    from localoffice.runtime import TaskRunner
    from localoffice.storage import digest
    source=tmp_path/'facts.txt';source.write_text('Budget EUR 120')
    old_revision=digest(source.read_bytes())
    shutil.copyfile('tests/fixtures/pdf/text.pdf',tmp_path/'source.pdf')
    pdf_revision=digest((tmp_path/'source.pdf').read_bytes())
    actions=[{'tool':'read_file','path':'facts.txt'},
             {'tool':'read_pdf','path':'source.pdf','page':2},
             {'tool':'stage_markdown','path':'brief.md','text':'# Brief\nBudget EUR 120\n'},
             {'tool':'finish','text':'Staged for review'}]
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 500
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':json.dumps(actions.pop(0))}
            yield {'type':'finish','reason':'stop'}
    store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Use facts.txt and source.pdf page 2 to create brief.md')
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        result=neighborhood(store,'task:'+task,max_nodes=50,max_edges=100,max_chars=16000)
        edges=result['edges']
        assert any(e['relation']=='targets' and e['target']=='file:brief.md' for e in edges)
        for revision in (old_revision,pdf_revision):
            edge=next(e for e in edges if e['relation']=='read-source' and e['target']=='revision:'+revision)
            message_id=int(edge['provenance'].removeprefix('message:'))
            receipt=json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(message_id,)).fetchone()[0])
            assert receipt.get('revision',receipt.get('source_hash'))==revision
        assert any(e['relation']=='read-page' and e['target']==f'pdf:{pdf_revision}:2:read' for e in edges)
        assert not any(e['target']==f'pdf:{pdf_revision}:1:read' for e in edges)
        assert not (tmp_path/'brief.md').exists()
        # Reading a changed source records another dependency; old evidence is retained.
        source.write_text('Budget EUR 150')
        actions.extend([{'tool':'read_file','path':'facts.txt'},{'tool':'finish','text':'Read'}])
        second=await runner.start('Read changed facts');await runner.job
        assert store.db.execute("SELECT 1 FROM edges WHERE source=? AND target=? AND relation='read-source'",('task:'+task,'revision:'+old_revision)).fetchone()
        assert store.db.execute("SELECT 1 FROM edges WHERE source=? AND target=? AND relation='read-source'",('task:'+second,'revision:'+digest(source.read_bytes()))).fetchone()
        store.close();store=ProjectStore(tmp_path)
        assert neighborhood(store,'task:'+task)['nodes']
        select_chat(store,create_chat(store,'Other'))
        assert neighborhood(store,'task:'+task)['nodes']==[]
        assert not any(n['kind']=='task' for n in neighborhood(store,'revision:'+old_revision)['nodes'])
    finally:store.close()
