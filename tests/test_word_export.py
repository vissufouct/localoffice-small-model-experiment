import json
import pytest
from fastapi.testclient import TestClient
from localoffice.server import Host,create_app
from test_word_creation import design


def test_pdf_export_preview_save_undo_and_exact_restore(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();(root/'exports').mkdir();host=Host()
    with TestClient(create_app('test',host)) as client:
        client.headers['Authorization']='Bearer test'
        client.post('/project/open',json={'path':str(root)})
        word=host.runner.registry.word.create(**design(),idempotency_key='create')
        source={'path':'report.docx','revision':word['candidate_revision']}
        render=client.post('/word/render',json=source).json()
        body={**source,'pdf_hash':render['pdf_hash'],'destination':'exports/report.PDF','idempotency_key':'export'}
        response=client.post('/word/export-pdf',json=body)
        assert response.status_code==200,response.text
        op=response.json();assert op['tool']=='pdf.export' and op['candidate_hash']==render['pdf_hash']
        assert not (root/'exports/report.PDF').exists() and not (root/'report.docx').exists()
        assert client.post('/word/export-pdf',json=body).json()==op
        assert client.post('/word/export-pdf',json={**body,'idempotency_key':'different'}).json()['code']=='target_exists'
        pdf=client.get('/binary',params={'path':'exports/report.PDF'})
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-')
        apply={'operation_id':op['id'],'candidate_hash':op['candidate_hash'],'action':'save'}
        assert client.post('/apply',json=apply).status_code==200
        assert (root/'exports/report.PDF').read_bytes()==pdf.content
        removed=client.post('/undo/'+op['id'],json={}).json()
        assert client.post('/apply',json={'operation_id':removed['id'],'candidate_hash':removed['candidate_hash'],'action':'remove'}).status_code==200
        assert not (root/'exports/report.PDF').exists()
        restored=client.post('/undo/'+removed['id'],json={}).json()
        assert client.post('/apply',json={'operation_id':restored['id'],'candidate_hash':restored['candidate_hash'],'action':'save'}).status_code==200
        assert (root/'exports/report.PDF').read_bytes()==pdf.content
        assert not (root/'report.docx').exists()
        assert host.project().db.execute("SELECT 1 FROM edges WHERE relation='exported-from'").fetchone()


def test_pdf_export_rejects_wrong_render_stale_source_and_external_collision(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    root=tmp_path/'project';root.mkdir();host=Host()
    with TestClient(create_app('test',host)) as client:
        client.headers['Authorization']='Bearer test';client.post('/project/open',json={'path':str(root)})
        word=host.runner.registry.word.create(**design(),idempotency_key='create')
        source={'path':'report.docx','revision':word['candidate_revision']}
        render=client.post('/word/render',json=source).json()
        body={**source,'pdf_hash':render['pdf_hash'],'destination':'out.pdf','idempotency_key':'export'}
        assert client.post('/word/export-pdf',json={**body,'pdf_hash':'0'*64}).json()['code']=='stale_render'
        assert client.post('/word/export-pdf',json={**body,'revision':'0'*64}).json()['code']=='stale_revision'
        assert client.post('/word/export-pdf',json={**body,'destination':'../out.pdf'}).json()['code']=='path_denied'
        assert client.post('/word/export-pdf',json={**body,'destination':'out.docx'}).json()['code']=='format_unsupported'
        assert client.post('/word/export-pdf',json={**body,'destination':'missing/out.pdf'}).json()['code']=='not_found'
        (root/'out.pdf').write_bytes(b'external file')
        assert client.post('/word/export-pdf',json=body).json()['code']=='target_exists'
        assert (root/'out.pdf').read_bytes()==b'external file'
        body['destination']='new.pdf'
        op=client.post('/word/export-pdf',json=body).json()
        (root/'new.pdf').write_bytes(b'created after staging')
        assert client.post('/apply',json={'operation_id':op['id'],'candidate_hash':op['candidate_hash'],'action':'save'}).status_code==409
        assert (root/'new.pdf').read_bytes()==b'created after staging'
