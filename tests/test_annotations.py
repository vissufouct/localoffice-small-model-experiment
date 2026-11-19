import asyncio
import json
import time
import pytest
from localoffice.annotations import annotate,bind_annotations
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.errors import HarnessError


@pytest.fixture
def store(tmp_path):
    (tmp_path/'note.md').write_text('# Heading\n\nTarget paragraph.\n\nKeep this unchanged.\n')
    s=ProjectStore(tmp_path)
    yield s
    s.close()


def setup_task(store,scope='selection',quote='Target paragraph.'):
    revision=store.file_hash('note.md')
    annotation=annotate(store,path='note.md',revision=revision,scope=scope,locator={'quote':quote},comment='Improve this.')
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('task','Edit the selection','running','live','note.md',time.time(),time.time()))
        bind_annotations(store,'task','note.md',[annotation])
    return revision,annotation


def test_selected_edit_preserves_unrelated_bytes(store):
    revision,_=setup_task(store)
    old=store.read('note.md')['text']
    changed=old.replace('Target paragraph.','A much longer revised paragraph.')
    op=store.stage(path='note.md',text=changed,expected_revision=revision,idempotency_key='one',task_id='task')
    assert store.read('note.md')['text']==changed
    assert (store.root/'note.md').read_text()==old
    # Multiple refinement steps retain the original host-owned boundary.
    refined=changed.replace('A much longer revised paragraph.','Short revised text.')
    store.stage(path='note.md',text=refined,expected_revision=op['candidate_hash'],idempotency_key='two',task_id='task')
    assert store.read('note.md')['text']==refined


def test_model_cannot_broaden_annotation(store):
    revision,_=setup_task(store)
    old=store.read('note.md')['text']
    with pytest.raises(HarnessError,match='outside'):
        store.stage(path='note.md',text=old.replace('Heading','New title'),expected_revision=revision,idempotency_key='bad',task_id='task')
    assert store.candidate('note.md') is None
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    assert store.file_hash('note.md')==revision


def test_whole_artifact_requires_explicit_user_scope(store):
    revision,_=setup_task(store,scope='artifact')
    op=store.stage(path='note.md',text='# Entire redesign\n',expected_revision=revision,idempotency_key='whole',task_id='task')
    assert op['status']=='validated'


def test_stale_annotation_rejected_before_task_binding(store):
    revision=store.file_hash('note.md')
    key=annotate(store,path='note.md',revision=revision,scope='selection',locator={'quote':'Target paragraph.'},comment='Change')
    (store.root/'note.md').write_text('external change')
    with pytest.raises(HarnessError,match='older revision'):
        with store.transaction():bind_annotations(store,'task','note.md',[key])
    assert store.db.execute('SELECT count(*) FROM task_annotations').fetchone()[0]==0


def test_duplicate_preview_quote_is_not_silently_redirected(store):
    (store.root/'note.md').write_text('same\nother\nsame')
    revision=store.file_hash('note.md')
    with pytest.raises(HarnessError,match='one exact'):
        annotate(store,path='note.md',revision=revision,scope='selection',locator={'quote':'same','start':0,'end':4},comment='Change second occurrence')
    key=annotate(store,path='note.md',revision=revision,scope='selection',locator={'quote':'same','start':11,'end':15,'offset_encoding':'unicode_codepoints'},comment='Change second occurrence')
    assert key


def test_unicode_selection_and_empty_suffix(store):
    (store.root/'note.md').write_text('🌲 Keep. Café final.')
    revision=store.file_hash('note.md')
    key=annotate(store,path='note.md',revision=revision,scope='selection',locator={'quote':'Café final.'},comment='Change')
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('task','Edit','running','live','note.md',0,0))
        bind_annotations(store,'task','note.md',[key])
    store.stage(path='note.md',text='🌲 Keep. Novo final.',expected_revision=revision,idempotency_key='unicode',task_id='task')
    assert store.read('note.md')['text']=='🌲 Keep. Novo final.'


class FinishingProvider:
    context_limit=4096
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 400
    async def stream(self,messages,**kwargs):
        yield {'type':'token','text':json.dumps({'tool':'finish','text':'Ready.'})}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.asyncio
async def test_pause_does_not_resurrect_completed_task(store):
    runner=TaskRunner(store,FinishingProvider())
    key=await runner.start('Read only','live','note.md')
    await runner.job
    await runner.pause()
    assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed'
    assert runner.active is None


@pytest.mark.asyncio
async def test_switching_artifact_selects_its_own_paused_task(store):
    runner=TaskRunner(store,FinishingProvider())
    with store.transaction():
        for key,path in [('first','note.md'),('second','other.md')]:
            store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(key,'Edit','paused','live',path,0,0))
            store.db.execute('INSERT INTO task_chats VALUES(?,?)',(key,runner.chat_id))
    runner.active='first'
    assert await runner.activate_artifact('other.md')=='second'
    with pytest.raises(HarnessError,match='cannot switch'):
        await runner.start('Continue','live','note.md','second')
    assert runner.active=='second'


@pytest.mark.asyncio
async def test_failed_edit_cannot_finish_as_completed(store):
    class Provider:
        context_limit=4096
        calls=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 300
        async def stream(self,messages,**kwargs):
            self.calls+=1
            text='{"tool":"stage_markdown","path":"new.md","text":"draft","expected_revision":"wrong-source-hash"}' if self.calls==1 else '{"tool":"finish","text":"Done"}'
            yield {'type':'token','text':text}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    key=await runner.start('Create new.md',artifact='new.md');await runner.job
    assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='failed'
    assert not any(event['type']=='completed' for event in runner.events)
    assert store.candidate('new.md') is None


@pytest.mark.asyncio
async def test_switch_running_artifact_preserves_candidate_feedback_and_task(store):
    """A deterministic streaming fixture tests the switch boundary, not model quality."""
    original=(store.root/'note.md').read_text()
    changed=original.replace('Target paragraph.','Revised paragraph.')
    (store.root/'other.md').write_text('Other original')
    entered=asyncio.Event()
    class Provider(FinishingProvider):
        calls=0
        async def stream(self,messages,**kwargs):
            self.calls+=1
            if self.calls==1:
                yield {'type':'token','text':json.dumps({'tool':'stage_markdown','path':'note.md','text':changed,'expected_revision':store.file_hash('note.md')})}
                yield {'type':'finish','reason':'stop'}
            else:
                entered.set()
                await asyncio.Event().wait()
                yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    first=await runner.start('Revise note.md',artifact='note.md')
    try:
        await asyncio.wait_for(entered.wait(),3)
        candidate=store.candidate('note.md')
        assert candidate and runner.state()['running']
        # Looking at a source is allowed without selecting a second editing target.
        assert await runner.activate_artifact('source.pdf')==first
        assert runner.state()['running']
        assert await runner.activate_artifact('other.md') is None
        assert runner.events[-1]['type']=='settled'
        assert runner.events[-1]['status']=='idle'
        assert not runner.state()['running']
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(first,)).fetchone()[0]=='paused'
        feedback=annotate(store,path='note.md',revision=candidate['candidate_hash'],scope='selection',locator={'quote':'Revised paragraph.'},comment='Refine this later.')
        assert await runner.activate_artifact('note.md')==first
        assert runner.events[-1]['type']=='settled'
        assert runner.events[-1]['status']=='paused'
        assert store.snapshot()['active_task_id']==first
        assert store.candidate('note.md')==candidate
        assert store.db.execute('SELECT 1 FROM annotations WHERE id=?',(feedback,)).fetchone()
        assert (store.root/'note.md').read_text()==original
        assert (store.root/'other.md').read_text()=='Other original'
        assert not store.db.execute("SELECT 1 FROM operations WHERE status='committed'").fetchone()
    finally:await runner.pause()
