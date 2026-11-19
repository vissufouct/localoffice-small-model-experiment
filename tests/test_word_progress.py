import json
import pytest
from localoffice.chats import current_chat
from localoffice.runtime import TaskRunner
from localoffice.word.progress import completed_changes
from localoffice.storage import canonical,ProjectStore
from localoffice.word.plugin import WordPlugin
from test_word_tables import saved_table


def task(store,key='task'):
    chat=current_chat(store)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(key,'Center table','running','background','report.docx',1,1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)',(key,chat))
    return chat


def change(plugin,rev,key,properties):
    target=plugin.read_objects(path='report.docx',revision=rev,ids=['table:1'])['objects'][0]
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'format','op':'format_table','target':target['id'],'expected_hash':target['hash'],'properties':properties}],idempotency_key=key,task_id='task')


def test_progress_comes_from_ledger_is_scoped_and_survives_restart(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store)
    first=change(plugin,rev,'one',{'alignment':'center'})
    second=change(plugin,first['candidate_revision'],'two',{'padding_top_twips':120})
    store.message('assistant','I also deleted all content.','task')
    progress=completed_changes(store,'task',chat,'report.docx',second['candidate_revision'])
    assert len(progress['receipts'])==2
    assert progress['receipts'][0]['matches_current_revision'] is True
    assert progress['receipts'][1]['matches_current_revision'] is False
    assert progress['receipts'][0]['changes'][0]['properties']=={'padding_top_twips':120}
    assert 'deleted' not in canonical(progress)
    assert completed_changes(store,'other',chat,'report.docx',rev) is None
    assert completed_changes(store,'task','other-chat','report.docx',rev) is None
    assert completed_changes(store,'task',chat,'other.docx',rev) is None
    store.close();store=ProjectStore(tmp_path)
    try:
        assert completed_changes(store,'task',chat,'report.docx',second['candidate_revision'])==progress
        # Historical completion never claims that an external or restored revision has those edits.
        stale=completed_changes(store,'task',chat,'report.docx',rev)
        assert not any(x['matches_current_revision'] for x in stale['receipts'])
    finally:store.close()


def test_progress_bounds_omit_large_values_without_truncating_history(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store)
    try:
        for i in range(5):
            target=plugin.read_objects(path='report.docx',revision=rev,ids=['cell:4'])['objects'][0]
            value='漢'*2000+str(i)
            result=plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'cell','op':'set_cell_text','target':target['id'],'expected_hash':target['hash'],'text':value}],idempotency_key=str(i),task_id='task')
            rev=result['candidate_revision']
        p=completed_changes(store,'task',chat,'report.docx',rev)
        assert len(canonical(p).encode())<=2000
        assert p['older_receipts_omitted']>=2 and p['receipts'][0]['details_omitted']
        assert all(x['changes']==[] for x in p['receipts'])
        args=json.loads(store.operation(result['receipt_id'])['args'])
        assert args['request']['operations'][0]['text']==value
    finally:store.close()


@pytest.mark.asyncio
async def test_progress_survives_forced_context_pruning(tmp_path):
    class Provider:
        context_limit=4096
        async def count_tokens(self,messages):return sum(len(m['content'])//4 for m in messages)
    store,plugin,original,rev=saved_table(tmp_path);chat=task(store)
    try:
        result=change(plugin,rev,'format',{'alignment':'center'})
        for i in range(20):store.message('user','OLD-EVIDENCE-'+str(i)+'x'*2000,'task')
        store.message('tool','{"verified_table_alignment":"center"}','task')
        runner=TaskRunner(store,Provider());messages=await runner.assemble('task')
        assert runner.context_info['omitted_messages']>0
        assert 'completed_word_changes' in messages[0]['content']
        assert result['receipt_id'] in messages[0]['content']
        assert '"alignment":"center"' in messages[0]['content']
        assert 'verified_table_alignment' in messages[-1]['content']
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('prune',[False,True])
async def test_context_prunes_whole_tool_pairs_and_repairs_window_boundary(tmp_path,prune):
    class Provider:
        context_limit=4096 if prune else 20000
        async def count_tokens(self,messages):return sum(len(m['content'])//4 for m in messages)
    store,plugin,original,rev=saved_table(tmp_path);task(store)
    try:
        # 17 messages means the 16-message window starts with a tool result.
        for i in range(8):
            store.message('assistant',f'CALL-{i} '+('x'*900),'task')
            store.message('tool',f'RESULT-{i} '+('y'*900),'task')
        store.message('user','Continue without repeating completed work.','task')
        runner=TaskRunner(store,Provider());messages=await runner.assemble('task');text=canonical(messages)
        for i in range(8):assert (f'CALL-{i} ' in text)==(f'RESULT-{i} ' in text)
        if prune:assert runner.context_info['omitted_messages']>0
        else:assert 'CALL-0 ' in text and 'RESULT-0 ' in text
        assert store.db.execute("SELECT count(*) FROM messages WHERE task_id='task'").fetchone()[0]==17
    finally:store.close()


def test_creation_style_grouping_is_lossless_and_does_not_mutate_receipts():
    import copy
    from localoffice.word.progress import group_creation_style_facts
    styles=[{'id':name,'fonts':{'ascii':'Arial','hAnsi':'Arial'},'size_half_points':'18'}
            for name in ('Header','Footer')]
    evidence={'styles':styles,'bands':[{'kind':'footer','sequence':[{'field':' PAGE '}]}],
              'body':[{'text':'Exact body'}],'geometry':{'width':11906}}
    source={'receipts':[{'matches_current_revision':True,'creation_verification':evidence},
                        {'matches_current_revision':False,'creation_verification':copy.deepcopy(evidence)}]}
    before=copy.deepcopy(source)
    result=group_creation_style_facts(source)
    assert source==before
    grouped=result['receipts'][0]['creation_verification']
    restored=[{'id':name,**{k:v for k,v in group.items() if k!='ids'}}
              for group in grouped['style_groups'] for name in group['ids']]
    assert restored==styles
    assert {k:v for k,v in grouped.items() if k!='style_groups'}=={k:v for k,v in evidence.items() if k!='styles'}
    assert result['receipts'][1]==source['receipts'][1]
