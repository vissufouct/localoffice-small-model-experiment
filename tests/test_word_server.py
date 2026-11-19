from fastapi.testclient import TestClient
from localoffice.server import Host,create_app
from test_word_creation import design


def test_word_preview_api_uses_exact_working_revision(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();host=Host()
    with TestClient(create_app('test',host)) as client:
        client.headers['Authorization']='Bearer test'
        assert client.post('/project/open',json={'path':str(root)}).status_code==200
        receipt=host.runner.registry.word.create(**design(),idempotency_key='create')
        body={'path':'report.docx','revision':receipt['candidate_revision']}
        objects=client.post('/word/objects',json={**body,'limit':1}).json()
        assert objects['objects'][0]['id']=='paragraph:1'
        obj=objects['objects'][0]
        annotated=client.post('/annotation',json={**body,'scope':'selection','locator':{'object_id':obj['id'],'object_hash':obj['hash']},'comment':'Improve this paragraph only'})
        assert annotated.status_code==200,annotated.text
        response=client.post('/word/render',json=body)
        assert response.status_code==200,response.text
        result=response.json();assert result['pages']==2
        pdf=client.get('/word/pdf/'+result['pdf_hash'],params=body)
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-')
        assert client.get('/word/pdf/'+result['pdf_hash'],params={**body,'revision':'0'*64}).status_code==409
        assert client.get('/word/pdf/'+'0'*64,params=body).status_code==409
        review={**body,'phase':'during','outcome':'accepted','note':'Visible page inspected',
                'view':{'renderer':'PDF.js','version':'test','pdf_hash':result['pdf_hash'],'page':1,'viewport_height':600}}
        recorded=client.post('/review/user',json=review)
        assert recorded.status_code==200,recorded.text
        assert recorded.json()['automated_vision'] is False
        review['view']['pdf_hash']='0'*64
        assert client.post('/review/user',json=review).json()['code']=='review_render'
        assert not (root/'report.docx').exists()
        host.runner.registry.set_enabled('word',False)
        assert client.post('/word/render',json=body).json()['code']=='plugin_disabled'


def test_word_render_blocks_switch_and_model_work(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();host=Host()
    with TestClient(create_app('test',host)) as client:
        client.headers['Authorization']='Bearer test'
        client.post('/project/open',json={'path':str(root)})
        host.runner.registry.word_renderer.busy=True
        try:
            assert client.post('/task',json={'text':'Write a note'}).json()['code']=='pdf_busy'
            assert client.post('/chats',json={'title':'Other'}).status_code==409
            assert client.post('/project/open',json={'path':str(root)}).status_code==409
            assert client.post('/model/load',json={'path':'unused'}).status_code==409
            assert client.post('/pdf/read',json={'path':'unused.pdf'}).json()['code']=='render_busy'
        finally:host.runner.registry.word_renderer.busy=False


def test_new_word_target_is_selection_only_and_refuses_collisions(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();host=Host()
    with TestClient(create_app('test',host)) as client:
        client.headers['Authorization']='Bearer test'
        client.post('/project/open',json={'path':str(root)})
        response=client.post('/word/new-target',json={'path':'drafts/Brief.DOCX'})
        assert response.status_code==200,response.text
        assert response.json()=={'path':'drafts/Brief.DOCX','active':None,'created':False}
        assert not (root/'drafts').exists()
        assert not host.project().candidate('drafts/Brief.DOCX')
        assert host.project().db.execute("SELECT value FROM meta WHERE key='active_artifact'").fetchone()[0]=='drafts/Brief.DOCX'
        (root/'existing.docx').write_bytes(b'original')
        assert client.post('/word/new-target',json={'path':'existing.docx'}).json()['code']=='target_exists'
        host.runner.registry.word.create(**design(),idempotency_key='new-target-fixture')
        assert client.post('/word/new-target',json={'path':'report.docx'}).json()['code']=='target_exists'
        for path in ['../escape.docx','.localoffice/hidden.docx','/tmp/escape.docx']:
            assert client.post('/word/new-target',json={'path':path}).json()['code']=='path_denied'
        assert client.post('/word/new-target',json={'path':'report.pdf'}).json()['code']=='format_unsupported'
        host.runner.registry.word_renderer.busy=True
        try:
            assert client.post('/word/new-target',json={'path':'busy.docx'}).json()['code']=='render_busy'
        finally:host.runner.registry.word_renderer.busy=False
        assert (root/'existing.docx').read_bytes()==b'original'
