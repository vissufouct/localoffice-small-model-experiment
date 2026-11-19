import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.assets import AssetPlugin
from localoffice.errors import HarnessError
from localoffice.word.contracts import StageBatch
from test_assets import picture
from test_word_tables import saved_table
from test_word_creation import W

WP='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'


def setup(root,orientation=None):
    store,plugin,original,rev=saved_table(root)
    image=picture(root/'photo.jpg','JPEG',orientation)
    asset=AssetPlugin(store).import_image(path='photo.jpg')
    target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
    op={'id':'figure','op':'insert_image','target':target['id'],'expected_hash':target['hash'],
        'asset_id':asset['asset_id'],'width_inches':2,'alt_text':'Blue sample <image> & description','caption':'Figure 1 — Sample'}
    return store,plugin,original,rev,image,op


def insert(plugin,rev,op,key='image'):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key=key)


def test_insert_image_preserves_package_bytes_aspect_alt_caption_and_exact_undo(tmp_path):
    store,plugin,original,rev,image,op=setup(tmp_path)
    try:
        result=insert(plugin,rev,op);assert insert(plugin,rev,op)==result
        assert set(result['results'][0]['inserted_ids'])=={'paragraph:3','image:1','paragraph:4'}
        data=store.blob(result['candidate_revision'])
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(data)) as new:
            media=[n for n in new.namelist() if n.startswith('word/media/')]
            assert len(media)==1 and new.read(media[0])==image
            for name in old.namelist():
                if name not in ('word/document.xml','word/_rels/document.xml.rels','[Content_Types].xml'):
                    assert old.read(name)==new.read(name),name
            doc=ET.fromstring(new.read('word/document.xml'));extent=doc.find('.//{'+WP+'}extent')
            assert extent.attrib=={'cx':'1828800','cy':'1219200'}
            assert doc.find('.//{'+WP+'}docPr').get('descr')==op['alt_text']
            paragraphs=doc.find('{'+W+'}body').findall('{'+W+'}p')
            assert ''.join(paragraphs[1].itertext())=='Evidence-backed introduction.'
            assert ''.join(paragraphs[3].itertext())==op['caption']
            olddoc=ET.fromstring(old.read('word/document.xml'))
            body=doc.find('{'+W+'}body');body.remove(paragraphs[2]);body.remove(paragraphs[3])
            assert ET.canonicalize(ET.tostring(olddoc))==ET.canonicalize(ET.tostring(doc))
        images=plugin.list_objects(path='report.docx',revision=result['candidate_revision'],kind='image')['objects']
        assert len(images)==1 and images[0]['preview']==op['alt_text']
        assert (tmp_path/'report.docx').read_bytes()==original
        assert store.db.execute("SELECT count(*) FROM edges WHERE source=? AND relation='placed-in'",(op['asset_id'],)).fetchone()[0]==1
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_oriented_asset_uses_oriented_copy_and_preserves_source(tmp_path):
    from PIL import Image
    store,plugin,original,rev,image,op=setup(tmp_path,6)
    try:
        result=insert(plugin,rev,op)
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as z:
            name=next(n for n in z.namelist() if n.startswith('word/media/'))
            with Image.open(io.BytesIO(z.read(name))) as p:assert p.size==(20,30) and p.format=='PNG'
            doc=ET.fromstring(z.read('word/document.xml'))
            assert doc.find('.//{'+WP+'}extent').get('cy')=='2743200'
        assert AssetPlugin(store).resolve(op['asset_id'])[1]==image
    finally:store.close()


def test_repeated_image_reuses_media_but_has_distinct_drawing_ids(tmp_path):
    store,plugin,original,rev,image,op=setup(tmp_path)
    try:
        first=insert(plugin,rev,op);second=insert(plugin,first['candidate_revision'],{**op,'position':'before'},'second')
        with zipfile.ZipFile(io.BytesIO(store.blob(second['candidate_revision']))) as z:
            assert len([n for n in z.namelist() if n.startswith('word/media/')])==1
            doc=ET.fromstring(z.read('word/document.xml'));ids=[p.get('id') for p in doc.findall('.//{'+WP+'}docPr')]
            assert len(ids)==len(set(ids))==2
    finally:store.close()


def test_image_insertion_rejects_missing_asset_stale_anchor_and_mixed_batch(tmp_path):
    store,plugin,original,rev,image,op=setup(tmp_path)
    try:
        with pytest.raises(HarnessError):insert(plugin,rev,{**op,'asset_id':'image:'+'0'*64})
        with pytest.raises(HarnessError,match='stale_object'):insert(plugin,rev,{**op,'expected_hash':'0'*64})
        with pytest.raises(ValidationError):StageBatch(path='report.docx',revision=rev,operations=[op,{**op,'id':'second','target':'paragraph:1'}])
        with store.transaction():store.db.execute("INSERT OR REPLACE INTO meta VALUES('plugin:assets','disabled')")
        with pytest.raises(HarnessError,match='disabled'):insert(plugin,rev,op)
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_image_insertion_cannot_expand_paragraph_selection_scope(tmp_path):
    import json
    from test_word_progress import task
    store,plugin,original,rev,image,op=setup(tmp_path)
    try:
        task(store)
        with store.transaction():
            store.db.execute('INSERT INTO task_scopes VALUES(?,?,?,?,?)',('task','report.docx',rev,rev,json.dumps({'format':'docx','objects':[op['target']]})))
        with pytest.raises(HarnessError,match='whole-artifact'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='scoped',task_id='task')
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()
