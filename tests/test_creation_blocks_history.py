from localoffice.word.plugin import WordPlugin
from localoffice.native_history import record_call,reconcile_word_call
from localoffice.memory import forget_project_memory
from test_word_content_plan import fixture


def test_only_executed_block_call_can_recover_and_forget_clears_receipts(tmp_path):
    store=fixture(tmp_path,'Create Body.')
    plugin=WordPlugin(store)
    try:
        proposal=plugin.prepare_creation(task_id='task',path='report.docx',blocks=[
            {'kind':'paragraph','text':'Body'},{'kind':'page_break'}])
        args={'ref':proposal['ref'],'changes':[{'op':'remove_block','index':1}]}
        call=record_call(store,'task',{'tool':'word.edit_creation_blocks',**args})
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        result=plugin.edit_creation_blocks(task_id='task',native_call_id=call,**args)
        assert result['status']=='prepared'
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        another=record_call(store,'task',{'tool':'word.edit_creation_blocks',**args})
        assert another!=call
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        assert store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'word_creation_block_result:%'").fetchone()
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        assert not store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'word_creation_block_result:%'").fetchone()
    finally:store.close()
