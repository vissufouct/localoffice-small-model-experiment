import pytest
from fastapi.testclient import TestClient
from localoffice.server import create_app,Host


def test_auth_and_project_tools(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    project=tmp_path/'project';project.mkdir()
    (project/'source.md').write_text('Source content')
    host=Host();app=create_app('test-session',host)
    with TestClient(app) as client:
        assert client.get('/health').status_code==401
        client.headers['Authorization']='Bearer test-session'
        assert client.get('/health').json()['status']=='ready'
        assert client.post('/project/open',json={'path':str(project)}).status_code==200
        files=client.get('/files').json()['files']
        assert [f['path'] for f in files]==['source.md']
        assert client.get('/file',params={'path':'source.md'}).json()['text']=='Source content'
        assert client.get('/file',params={'path':'../secret'}).status_code==409
        assert client.get('/file',params={'path':'.localoffice/state.sqlite3'}).status_code==409
        assert client.post('/task',json={'text':'Make a note'}).json()['code']=='model_disconnected'
        revision=client.get('/file',params={'path':'source.md'}).json()['revision']
        response=client.post('/annotation',json={'path':'source.md','revision':revision,'scope':'selection','locator':{'quote':'Source'},'comment':'Clarify this.'})
        assert response.status_code==200
        assert len(client.get('/project').json()['annotations'])==1
        assert client.post('/annotation',json={'path':'source.md','revision':'stale','scope':'artifact','locator':{},'comment':'Change'}).status_code==409


def test_pdf_password_never_echoed_in_validation_or_saved_state(tmp_path,monkeypatch):
    import shutil
    from pathlib import Path
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    project=tmp_path/'project';project.mkdir()
    shutil.copyfile(Path('tests/fixtures/pdf/encrypted.pdf'),project/'encrypted.pdf')
    host=Host()
    with TestClient(create_app('session',host)) as client:
        client.headers['Authorization']='Bearer session'
        client.post('/project/open',json={'path':str(project)})
        secret='fixture-password'
        for body in [ {'path':'encrypted.pdf','password':secret*100},
                      {'path':'encrypted.pdf','password':{'secret':secret}},
                      {'path':'encrypted.pdf','page':{'secret':secret},'password':secret} ]:
            response=client.post('/pdf/read',json=body)
            assert response.status_code==422
            assert secret not in response.text
        response=client.post('/pdf/read',content='{"password":"'+secret+'",',headers={'Content-Type':'application/json'})
        assert response.status_code==422 and secret not in response.text
        unlocked=client.post('/pdf/read',json={'path':'encrypted.pdf','password':secret})
        assert unlocked.status_code==200 and 'Cedar' in unlocked.json()['text']
        assert secret not in unlocked.text
        exported=client.get('/memory/export')
        assert secret not in exported.text
        wrong=client.post('/pdf/read',json={'path':'encrypted.pdf','password':'incorrect-private-password'})
        assert wrong.json()['code']=='pdf_encrypted'
        assert 'incorrect-private-password' not in wrong.text


def test_citation_resolution_requires_extracted_page_and_current_hash(tmp_path,monkeypatch):
    import shutil
    from pathlib import Path
    from localoffice.storage import digest
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();shutil.copyfile(Path('tests/fixtures/pdf/text.pdf'),root/'source.pdf')
    revision=digest((root/'source.pdf').read_bytes())
    with TestClient(create_app('token',Host())) as client:
        client.headers['Authorization']='Bearer token';client.post('/project/open',json={'path':str(root)})
        args={'path':'source.pdf','page':2,'revision':revision[:12]}
        assert client.get('/citation',params=args).json()['code']=='citation_unverified'
        assert client.post('/pdf/read',json={'path':'source.pdf','count':2}).status_code==200
        assert client.get('/citation',params=args).json()['revision']==revision
        assert client.get('/citation',params={**args,'page':3}).json()['code']=='citation_unverified'
        (root/'source.pdf').write_bytes(b'Changed externally')
        assert client.get('/citation',params=args).json()['code']=='citation_stale'


def test_preview_monitor_detects_external_changes_without_replacing_candidate(tmp_path,monkeypatch):
    from localoffice.storage import digest
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();source=root/'note.md';source.write_text('Original')
    host=Host()
    with TestClient(create_app('token',host)) as client:
        client.headers['Authorization']='Bearer token'
        client.post('/project/open',json={'path':str(root)})
        def state(path='note.md'):
            return client.get('/file/revision',params={'path':path})
        original=digest(b'Original')
        assert state().json()=={'original_revision':original,'preview_revision':original,'conflict':False}
        pending=host.project().stage(path='note.md',text='Candidate',expected_revision=original,idempotency_key='preview-test')
        source.write_text('External revision')
        assert state().json()=={'original_revision':digest(b'External revision'),'preview_revision':pending['candidate_hash'],'conflict':True}
        assert host.project().read('note.md')['text']=='Candidate'
        source.unlink()
        assert state().json()['original_revision'] is None
        assert state().json()['conflict'] is True
        assert state('../outside.md').status_code==409
        assert state('.localoffice/state.sqlite3').status_code==409
        fresh=root/'new.md';fresh.write_text('First')
        assert state('new.md').json()['preview_revision']==digest(b'First')
        fresh.write_text('Second')
        assert state('new.md').json()['preview_revision']==digest(b'Second')
        fresh.unlink()
        assert state('new.md').json()=={'original_revision':None,'preview_revision':None,'conflict':False}


def test_decision_review_correction_history_and_scope(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();host=Host()
    with TestClient(create_app('token',host)) as client:
        client.headers['Authorization']='Bearer token'
        first=client.post('/project/open',json={'path':str(root)}).json()['chat_id']
        message=host.project().message('user','Private budget draft is EUR 100.')
        data={'content':'Private budget draft EUR 100','source_message':message,'scope':'chat'}
        created=client.post('/decision',json=data)
        assert created.status_code==200
        key=created.json()['id']
        assert client.get('/decisions').json()[0]['versions'][0]['content']==data['content']
        confirmed=host.project().message('user','Confirmed shared budget is EUR 120.')
        correction={'content':'Confirmed budget EUR 120','source_message':confirmed,'scope':'project','decision_id':key,'expected_version':1}
        assert client.post('/decision',json=correction).status_code==200
        assert client.post('/decision',json=correction).json()['code']=='decision_stale'
        versions=client.get('/decisions').json()[0]['versions']
        assert [v['version'] for v in versions]==[2,1]
        private=client.post('/decision',json=data).json()['id']
        client.post('/chats',json={'title':'Separate chat'})
        rows=client.get('/decisions').json()
        assert len(rows)==1 and rows[0]['id']==key
        assert [v['version'] for v in rows[0]['versions']]==[2]
        assert 'Private budget draft' not in str(rows)
        assert client.post('/decision',json={**correction,'expected_version':2}).json()['code']=='chat_scope'
        own=host.project().message('user','Budget remains EUR 120.')
        assert client.post('/decision',json={**correction,'source_message':own,'decision_id':private}).json()['code']=='chat_scope'
        assert client.post('/decision',json={**correction,'source_message':own,'expected_version':None}).json()['code']=='decision_version'
        client.post('/chats/'+first+'/open',json={})
        assert len(client.get('/decisions').json())==2
        assert host.project().snapshot()['decisions'][0]['version']==2


def test_manual_pdf_and_model_work_cannot_overlap(tmp_path,monkeypatch):
    """Admission checks happen before any worker launch or model task mutation."""
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir()
    host=Host()
    with TestClient(create_app('token',host)) as client:
        client.headers['Authorization']='Bearer token'
        client.post('/project/open',json={'path':str(root)})
        host.loading=True
        for route in ('/pdf/read','/pdf/render'):
            response=client.post(route,json={'path':'source.pdf'})
            assert response.status_code==409 and response.json()['code']=='model_busy'
        host.loading=False
        original_state=host.runner.state
        monkeypatch.setattr(host.runner,'state',lambda:{'running':True})
        for route in ('/pdf/read','/pdf/render'):
            response=client.post(route,json={'path':'source.pdf'})
            assert response.status_code==409 and response.json()['code']=='task_busy'
        monkeypatch.setattr(host.runner,'state',original_state)
        host.runner.registry.pdf.busy=True
        assert client.post('/task',json={'text':'Create a note.'}).json()['code']=='pdf_busy'
        assert client.post('/model/load',json={'path':'missing.gguf','backend':'llamacpp'}).json()['code']=='pdf_busy'
        assert not host.loading and host.runner.active is None
        host.runner.registry.pdf.busy=False
        # With admission released, the same call reaches normal file validation.
        assert client.post('/pdf/read',json={'path':'source.pdf'}).json()['code']=='not_found'


def test_history_window_api_checks_chat_scope(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();host=Host()
    with TestClient(create_app('token',host)) as client:
        client.headers['Authorization']='Bearer token'
        client.post('/project/open',json={'path':str(root)})
        anchor=host.store.message('user','Scoped history anchor')
        result=client.get('/history/window',params={'message_id':anchor}).json()
        assert result['messages'][0]['content']=='Scoped history anchor'
        assert client.get('/history/window',params={'message_id':anchor,'radius':6}).json()['code']=='history_limit'
        client.post('/chats',json={'title':'Other chat'})
        assert client.get('/history/window',params={'message_id':anchor}).json()['code']=='not_found'
