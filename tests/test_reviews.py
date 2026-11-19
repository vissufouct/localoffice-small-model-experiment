import pytest
from fastapi.testclient import TestClient
from localoffice.server import create_app,Host
from localoffice.storage import ProjectStore,digest
from localoffice.reviews import record_review,list_reviews
from localoffice.errors import HarnessError

VIEW={'renderer':'marked+DOMPurify','version':'test','page':None,'zoom':1,'scroll_top':0,'viewport_height':600}


def test_review_tracks_exact_working_revision_and_survives_restart(tmp_path):
    store=ProjectStore(tmp_path)
    operation=store.stage(path='draft.md',text='# Draft',expected_revision=None,idempotency_key='draft')
    revision=operation['candidate_hash']
    receipt=record_review(store,path='draft.md',revision=revision,phase='during',outcome='needs_changes',note='Heading too large',view=VIEW)
    assert receipt['reviewer']=='user' and receipt['automated_vision'] is False
    assert list_reviews(store,'draft.md')[0]['current_revision']
    store.stage(path='draft.md',text='## Draft',expected_revision=revision,idempotency_key='revision')
    assert not list_reviews(store,'draft.md')[0]['current_revision']
    with pytest.raises(HarnessError) as error:
        record_review(store,path='draft.md',revision=revision,phase='final',outcome='accepted',note='',view=VIEW)
    assert error.value.code=='stale_revision'
    assert len(list_reviews(store,'draft.md'))==1
    store.close()
    store=ProjectStore(tmp_path)
    assert list_reviews(store,'draft.md')[0]['note']=='Heading too large'
    assert store.blob(revision)==b'# Draft'
    store.close()


def test_review_api_cannot_claim_model_vision(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();(root/'source.md').write_text('Text')
    with TestClient(create_app('token',Host())) as client:
        client.headers['Authorization']='Bearer token'
        client.post('/project/open',json={'path':str(root)})
        body={'path':'source.md','revision':digest(b'Text'),'phase':'before','outcome':'accepted','note':'Test attestation','view':VIEW}
        assert client.post('/review/user',json={**body,'reviewer':'model'}).status_code==422
        assert client.post('/review/user',json=body).status_code==200
        assert client.get('/reviews',params={'path':'source.md'}).json()[0]['automated_vision'] is False
        (root/'source.md').write_text('Externally changed')
        assert client.post('/review/user',json=body).json()['code']=='stale_revision'
        assert not client.get('/reviews',params={'path':'source.md'}).json()[0]['current_revision']
