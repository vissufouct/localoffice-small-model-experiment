import json
import pytest
from fastapi.testclient import TestClient
from localoffice.server import create_app,Host
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.chats import create_chat,select_chat,current_chat
from localoffice.attachments import import_attachment
from localoffice.errors import HarnessError


class Provider:
    context_limit=4096
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 400
    async def stream(self,messages,**kwargs):
        yield {'type':'token','text':'{"tool":"finish","text":"Ready."}'}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.asyncio
async def test_chat_context_isolated_but_project_decisions_shared(tmp_path):
    store=ProjectStore(tmp_path);first=current_chat(store)
    private_message=store.message('user','Private first-chat phrase mango')
    private_decision=store.decision('Mango is private',private_message)
    shared_message=store.message('user','Use British spelling throughout this project')
    store.decision('Use British spelling',shared_message,scope='project')
    second=create_chat(store,'Second');select_chat(store,second)
    store.message('user','Second-chat phrase kiwi')
    runner=TaskRunner(store,Provider())
    key=await runner.start('Continue our discussion')
    await runner.job
    context=json.dumps(await runner.assemble(key))
    assert 'kiwi' in context and 'British spelling' in context
    assert 'mango' not in context.lower()
    assert store.search('mango')==[]
    assert store.snapshot()['chat_id']==second
    assert all('mango' not in m['content'].lower() for m in store.snapshot()['messages'])
    with pytest.raises(HarnessError,match='another chat'):
        store.decision('overwrite',store.message('user','Overwrite'),private_decision)
    select_chat(store,first)
    assert store.search('mango')
    store.close()


def test_shared_working_copy_keeps_chat_ownership(tmp_path):
    store=ProjectStore(tmp_path)
    first=current_chat(store)
    op=store.stage(path='draft.md',text='# One',expected_revision=None,idempotency_key='first')
    second=create_chat(store,'Second');select_chat(store,second)
    assert store.snapshot()['candidates'][0]['owner_chat']['id']==first
    with pytest.raises(HarnessError,match='belongs to chat'):
        store.stage(path='draft.md',text='# Two',expected_revision=op['candidate_hash'],idempotency_key='second')
    with pytest.raises(HarnessError,match='owning chat'):
        store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    select_chat(store,first)
    store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    select_chat(store,second)
    assert store.read('draft.md')['text']=='# One'
    revised=store.stage(path='draft.md',text='# Shared revision',expected_revision=op['candidate_hash'],idempotency_key='revised')
    assert store.snapshot()['candidates'][0]['owner_chat']['id']==second
    store.close()


def test_attachments_copied_once_and_chat_scoped(tmp_path):
    source=tmp_path/'original.txt';source.write_text('Original evidence')
    project=tmp_path/'project';project.mkdir()
    store=ProjectStore(project);first=current_chat(store)
    item=import_attachment(store,'original.txt',source.read_bytes())
    assert item['path'].startswith('input/'+first+'/')
    assert (project/item['path']).read_bytes()==source.read_bytes()
    assert store.operation(item['id'])['status']=='committed'
    assert import_attachment(store,'original.txt',source.read_bytes())['id']==item['id']
    assert len(store.snapshot()['attachments'])==1
    second=create_chat(store,'Second');select_chat(store,second)
    assert store.snapshot()['attachments']==[]
    assert (project/item['path']).exists()  # Shared project visibility, no automatic context attachment.
    assert source.read_text()=='Original evidence'
    store.close()
    store=ProjectStore(project)
    select_chat(store,first)
    assert store.snapshot()['attachments'][0]['id']==item['id']
    store.close()


def test_attachment_rejects_executable_and_preserves_conflict(tmp_path):
    store=ProjectStore(tmp_path)
    with pytest.raises(HarnessError,match='executables'):import_attachment(store,'run.py',b'print(1)')
    item=import_attachment(store,'source.md',b'original')
    (tmp_path/item['path']).write_text('changed by user')
    with pytest.raises(HarnessError,match='changed'):import_attachment(store,'source.md',b'original')
    assert (tmp_path/item['path']).read_text()=='changed by user'
    store.close()


def test_project_chat_attachment_api(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    host=Host()
    with TestClient(create_app('token',host)) as client:
        client.headers['Authorization']='Bearer token'
        path=tmp_path/'Project A'
        response=client.post('/projects',json={'name':'Project A','path':str(path),'create_folder':True})
        assert response.status_code==200
        first=response.json()['chat_id']
        assert client.get('/projects').json()[0]['name']=='Project A'
        source=client.post('/attachments',params={'filename':'facts.txt'},content=b'Facts for chat one',headers={'Content-Type':'application/octet-stream'})
        assert source.status_code==200,source.text
        assert len(client.get('/attachments').json())==1
        new=client.post('/chats',json={'title':'Independent chat'}).json()
        assert new['chat_id']!=first and new['attachments']==[] and new['messages']==[]
        restored=client.post('/chats/'+first+'/open',json={}).json()
        assert len(restored['attachments'])==1
        files=client.get('/files').json()['files']
        assert any(f['path']==source.json()['path'] for f in files)
        assert len(client.get('/projects').json()[0]['chats'])==2


def test_interrupted_upload_retries_same_operation(tmp_path,monkeypatch):
    store=ProjectStore(tmp_path)
    original_put=store.put_blob
    def interrupted(data):
        raise OSError('simulated interruption before source blob')
    monkeypatch.setattr(store,'put_blob',interrupted)
    with pytest.raises(OSError):
        import_attachment(store,'evidence.txt',b'Preserved evidence')
    operation=dict(store.db.execute("SELECT * FROM operations WHERE tool='source.import'").fetchone())
    assert operation['status']=='planned'
    monkeypatch.setattr(store,'put_blob',original_put)
    item=import_attachment(store,'evidence.txt',b'Preserved evidence')
    assert item['id']==operation['id']
    assert (tmp_path/item['path']).read_bytes()==b'Preserved evidence'
    assert store.db.execute("SELECT count(*) FROM operations WHERE tool='source.import'").fetchone()[0]==1
    store.close()
