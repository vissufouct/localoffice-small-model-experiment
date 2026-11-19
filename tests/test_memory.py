import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.memory import export_memory,start_fresh
from localoffice.chats import create_chat,select_chat
from localoffice.runtime import TaskRunner


def test_export_contains_full_history_and_all_chats(tmp_path):
    store=ProjectStore(tmp_path)
    for i in range(215):store.message('user',f'First chat message {i}')
    select_chat(store,create_chat(store,'Second'))
    store.message('user','Second chat message')
    path=export_memory(store)
    try:
        data=json.loads(path.read_text())
        assert len(data['tables']['messages'])==216
        assert len(data['tables']['chats'])==2
        assert 'history_fts' not in data['tables']
        assert data['scope']=='all project chats'
    finally:path.unlink();store.close()


def test_decision_correction_preserves_prior_evidence(tmp_path):
    store=ProjectStore(tmp_path)
    first=store.message('user','Use British spelling')
    key=store.decision('British spelling',first)
    second=store.message('user','Use American spelling instead')
    store.decision('American spelling',second,key)
    versions=[dict(r) for r in store.db.execute('SELECT * FROM decision_versions ORDER BY version')]
    assert [v['source_message'] for v in versions]==[first,second]
    assert [v['content'] for v in versions]==['British spelling','American spelling']
    assert store.snapshot()['decisions'][0]['content']=='American spelling'
    store.close()


@pytest.mark.asyncio
async def test_fresh_context_excludes_old_chat_but_retains_decisions(tmp_path):
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 300
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':'{"tool":"finish","text":"Done"}'}
            yield {'type':'finish','reason':'stop'}
    store=ProjectStore(tmp_path)
    old=store.message('user','Obsolete conversation about mango')
    store.decision('Use source citations',old)
    start_fresh(store)
    runner=TaskRunner(store,Provider());task=await runner.start('New task about kiwi');await runner.job
    context=json.dumps(await runner.assemble(task))
    assert 'mango' not in context and 'kiwi' in context and 'Use source citations' in context
    assert store.search('mango')
    store.close()


def test_restart_preserves_explicit_fresh_state_and_unfinished_task(tmp_path):
    from localoffice.chats import current_chat,set_active_task
    store=ProjectStore(tmp_path);chat=current_chat(store)
    with store.transaction():
        store.db.execute("INSERT INTO tasks VALUES('old','Unfinished draft','running','live','draft.md',1,1)")
        store.db.execute("INSERT INTO task_chats VALUES('old',?)",(chat,))
    set_active_task(store,'old')
    store.close()
    store=ProjectStore(tmp_path);runner=TaskRunner(store,object())
    assert runner.active=='old'
    assert store.snapshot()['active_task_id']=='old'
    assert store.db.execute("SELECT status FROM tasks WHERE id='old'").fetchone()[0]=='paused'
    start_fresh(store);store.close()
    store=ProjectStore(tmp_path);runner=TaskRunner(store,object())
    assert runner.active is None
    assert store.snapshot()['active_task_id'] is None
    assert store.db.execute("SELECT objective FROM tasks WHERE id='old'").fetchone()[0]=='Unfinished draft'
    store.close()


def test_task_focus_is_chat_scoped(tmp_path):
    from localoffice.chats import current_chat,set_active_task,active_task
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path);first=current_chat(store)
    with store.transaction():
        store.db.execute("INSERT INTO tasks VALUES('a','Draft','paused','live','draft.md',1,1)")
        store.db.execute("INSERT INTO task_chats VALUES('a',?)",(first,))
    set_active_task(store,'a')
    second=create_chat(store,'Second');select_chat(store,second)
    assert active_task(store) is None
    with pytest.raises(HarnessError):set_active_task(store,'a')
    start_fresh(store)
    select_chat(store,first)
    assert active_task(store)=='a'
    store.close()


def test_forget_removes_content_indexes_projections_and_backups(tmp_path):
    from localoffice.memory import forget_project_memory
    from localoffice.attachments import import_attachment
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path)
    secret='private-memory-marker-9472'
    source=store.message('user',secret)
    store.decision(secret,source)
    imported=import_attachment(store,'source.txt',b'User-owned input evidence')
    op=store.stage(path='draft.md',text=secret,expected_revision=None,idempotency_key='write')
    store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    store.checkpoint(summary=secret)
    projection_prefixes=('word_render_context:','word_read_context:','word_active_literal_plan:','word_content_coverage:',
                         'word_creation_repair:','word_tools:','word_creation_submission:',
                         'word_requirements:','word_requirements_history:','word_requirements_result:',
                           'draft_fragment:','draft_fragment_history:','draft_fragment_result:',
                           'task_draft_budget:')
    with store.transaction():
        for prefix in projection_prefixes:
            store.db.execute('INSERT INTO meta VALUES(?,?)',(prefix+'test-task',secret))
        store.db.execute('INSERT INTO meta VALUES(?,?)',('model_preference','keep-setting'))
    with pytest.raises(HarnessError):forget_project_memory(store,'incorrect')
    result=forget_project_memory(store,'FORGET PROJECT MEMORY')
    assert result['status']=='forgotten'
    for prefix in projection_prefixes:
        assert not store.db.execute('SELECT 1 FROM meta WHERE key LIKE ?',(prefix+'%',)).fetchone()
    assert store.db.execute("SELECT value FROM meta WHERE key='model_preference'").fetchone()[0]=='keep-setting'
    assert store.search(secret)==[]
    assert not store.snapshot()['decisions'] and not store.snapshot()['messages']
    assert not list(store.blobs.iterdir())
    assert (tmp_path/'draft.md').read_text()==secret
    assert (tmp_path/imported['path']).read_bytes()==b'User-owned input evidence'
    exported=export_memory(store)
    try:assert secret not in exported.read_text()
    finally:exported.unlink()
    with pytest.raises(HarnessError,match='erased'):store.undo(op['id'],'undo')
    store.close();store=ProjectStore(tmp_path)
    assert not store.snapshot()['attachments']
    reattached=import_attachment(store,'source.txt',b'User-owned input evidence')
    assert reattached['id']!=imported['id']
    assert (tmp_path/reattached['path']).exists()
    store.close()


def test_forget_defers_pending_recovery_evidence(tmp_path):
    from localoffice.memory import forget_project_memory
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path)
    store.message('user','Keep pending evidence')
    op=store.stage(path='pending.md',text='Pending',expected_revision=None,idempotency_key='pending')
    with pytest.raises(HarnessError) as error:forget_project_memory(store,'FORGET PROJECT MEMORY')
    assert error.value.code=='deletion_deferred'
    assert store.search('pending')
    assert store.blob(op['candidate_hash'])==b'Pending'
    store.close()


def test_interrupted_forget_finishes_on_reopen(tmp_path,monkeypatch):
    import localoffice.memory as memory
    store=ProjectStore(tmp_path)
    store.message('user','Sensitive text awaiting erasure')
    blob=store.put_blob(b'Cached sensitive source')
    real_finish=memory.finish_forget
    def interrupted(store):raise RuntimeError('simulated interruption after deletion intent')
    monkeypatch.setattr(memory,'finish_forget',interrupted)
    with pytest.raises(RuntimeError):memory.forget_project_memory(store,'FORGET PROJECT MEMORY')
    assert (store.blobs/blob).exists()
    store.close()
    monkeypatch.setattr(memory,'finish_forget',real_finish)
    store=ProjectStore(tmp_path)
    assert not (store.blobs/blob).exists()
    assert not store.search('Sensitive')
    assert not store.db.execute("SELECT 1 FROM meta WHERE key='forget_pending'").fetchone()
    store.close()


def test_history_window_is_bounded_scoped_and_survives_fresh(tmp_path):
    from localoffice.chats import current_chat
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path)
    try:
        first=current_chat(store);second=create_chat(store,'Private other chat')
        ids=[]
        for i in range(12):
            ids.append(store.message('user',f'needle {i} '+('x'*3000 if i==6 else ''),chat_id=first))
            store.message('user','OTHER CHAT SECRET',chat_id=second)
        start_fresh(store)
        result=store.history_window(ids[6],2)
        assert [m['id'] for m in result['messages']]==ids[4:9]
        assert result['messages'][2]['truncated'] and len(result['messages'][2]['content'])==2000
        assert 'OTHER CHAT SECRET' not in json.dumps(result)
        assert len(store.history_window(ids[6],5)['messages'])==11
        assert len(store.history_window(ids[0],2)['messages'])==3
        assert len(store.history_window(ids[-1],0)['messages'])==1
        assert all(len(hit['content'])<=2000 for hit in store.search('needle'))
        with pytest.raises(HarnessError):store.history_window(ids[6],6)
        select_chat(store,second)
        with pytest.raises(HarnessError) as exc:store.history_window(ids[6])
        assert exc.value.code=='not_found'
        # Reading excerpts does not truncate the authoritative history.
        assert len(store.db.execute('SELECT content FROM messages WHERE id=?',(ids[6],)).fetchone()[0])>3000
    finally:store.close()


def test_forget_erases_real_draft_history_receipts_and_budget(tmp_path):
    from localoffice import draft_budget
    from localoffice.chats import current_chat
    from localoffice.memory import forget_project_memory
    from localoffice.native_history import record_call, record_result
    from localoffice.storage import digest
    from localoffice.word import draft_fragments

    store = ProjectStore(tmp_path)
    objective = 'Draft a private invitation.'
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                         ('draft-task', objective, 'running', 'background', 'report.docx', 1, 1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)', ('draft-task', current_chat(store)))
    draft_budget.reserve(store, 'draft-task', 10)
    args = {'source_hash': digest(objective.encode()),
            'source_range': {'start': 's01', 'end': 's01'}}
    call = record_call(store, 'draft-task', {'tool': 'word.plan_draft', **args})
    planned = draft_fragments.plan_draft(store, 'draft-task', **args, native_call_id=call)
    record_result(store, 'draft-task', call, planned)
    text = 'Private generated draft marker 52691.'
    args = {'ref': planned['ref'], 'text': text}
    call = record_call(store, 'draft-task', {'tool': 'word.submit_draft', **args})
    submitted = draft_fragments.submit_draft(store, 'draft-task', **args, native_call_id=call)
    record_result(store, 'draft-task', call, submitted)
    assert draft_fragments.get(store, 'draft-task')['text'] == text
    assert draft_budget.get(store, 'draft-task') == 1
    forget_project_memory(store, 'FORGET PROJECT MEMORY')
    store.close()
    store = ProjectStore(tmp_path)
    try:
        for prefix in (draft_fragments.PREFIX, draft_fragments.HISTORY_PREFIX,
                       draft_fragments.RESULT_PREFIX, draft_budget.PREFIX):
            assert not store.db.execute('SELECT 1 FROM meta WHERE key LIKE ?', (prefix + '%',)).fetchone()
        path = export_memory(store)
        try:
            exported = path.read_text()
            assert objective not in exported and text not in exported
        finally:
            path.unlink()
        assert not (tmp_path / 'report.docx').exists()
    finally:
        store.close()
