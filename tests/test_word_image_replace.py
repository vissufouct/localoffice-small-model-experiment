import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from PIL import Image
from pydantic import ValidationError
from localoffice.assets import AssetPlugin
from localoffice.errors import HarnessError
from localoffice.word.contracts import StageBatch
from test_word_image_format import placed,read,edit
from test_word_images import insert

A='{http://schemas.openxmlformats.org/drawingml/2006/main}'
R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def replacement(root,store):
    Image.new('RGB',(70,40),'gold').save(root/'new.png')
    return AssetPlugin(store).import_image(path='new.png')['asset_id']


def replace(plugin,rev,target,asset,key='replace'):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'swap','op':'replace_image','target':target['id'],'expected_hash':target['hash'],'asset_id':asset}],idempotency_key=key)


def test_replace_preserves_drawing_and_shared_original_media_with_exact_undo(tmp_path):
    store,plugin,original,rev=placed(tmp_path)
    try:
        # Create a second picture sharing the original media; crop the first.
        oldasset=AssetPlugin(store).import_image(path='photo.jpg')['asset_id']
        anchor=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        staged=insert(plugin,rev,{'id':'second','op':'insert_image','target':anchor['id'],'expected_hash':anchor['hash'],'asset_id':oldasset,'width_inches':2,'alt_text':'Second picture'},'second')
        rev=staged['candidate_revision'];cropped=edit(plugin,rev,read(plugin,rev),{'crop_left_percent':10},'crop')
        store.apply(cropped['receipt_id'],expected_candidate=cropped['candidate_revision'],action='apply')
        original=(tmp_path/'report.docx').read_bytes();rev=cropped['candidate_revision'];target=read(plugin,rev)
        asset=replacement(tmp_path,store);result=replace(plugin,rev,target,asset)
        assert replace(plugin,rev,target,asset)==result
        current=read(plugin,result['candidate_revision'])
        assert current['direct_format']==target['direct_format']
        data=store.blob(result['candidate_revision'])
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(data)) as new:
            for name in old.namelist():
                if name not in ('word/document.xml','word/_rels/document.xml.rels','[Content_Types].xml'):
                    assert old.read(name)==new.read(name),name
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            ab=list(a.iter(A+'blip'));bb=list(b.iter(A+'blip'))
            assert ab[1].get(R+'embed')==bb[1].get(R+'embed')
            assert ab[0].get(R+'embed')!=bb[0].get(R+'embed')
            bb[0].set(R+'embed',ab[0].get(R+'embed'))
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
            added=set(new.namelist())-set(old.namelist());assert len(added)==1
            assert new.read(next(iter(added)))==(tmp_path/'new.png').read_bytes()
        assert (tmp_path/'report.docx').read_bytes()==original
        assert replace(plugin,result['candidate_revision'],current,asset,'noop')['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-replace');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_replace_rejects_missing_assets_stale_targets_wrong_targets_and_mixed_batches(tmp_path):
    store,plugin,original,rev=placed(tmp_path)
    try:
        target=read(plugin,rev);asset=replacement(tmp_path,store)
        with pytest.raises(HarnessError):replace(plugin,rev,target,'image:'+'0'*64)
        with pytest.raises(HarnessError):replace(plugin,rev,{**target,'hash':'0'*64},asset)
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError,match='image target'):replace(plugin,rev,paragraph,asset)
        with pytest.raises(ValidationError):StageBatch(path='report.docx',revision=rev,operations=[{'id':str(i),'op':'replace_image','target':f'image:{i}','expected_hash':target['hash'],'asset_id':asset} for i in (1,2)])
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_identical_replacement_with_foreign_media_filename_is_noop(tmp_path):
    from test_word_lists import rewrite
    store,plugin,original,rev=placed(tmp_path)
    try:
        asset=AssetPlugin(store).import_image(path='photo.jpg')['asset_id']
        def rename(parts):
            name=next(n for n in parts if n.startswith('word/media/'))
            new='word/media/external-photo.jpg';parts[new]=parts.pop(name)
            for part in ('word/_rels/document.xml.rels','[Content_Types].xml'):
                parts[part]=parts[part].replace(name.encode(),new.encode())
        rewrite(tmp_path,original,rename);original=(tmp_path/'report.docx').read_bytes();rev=store.file_hash('report.docx')
        assert replace(plugin,rev,read(plugin,rev),asset)['status']=='unchanged'
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_replacement_schema_is_lazy_and_effects_are_rejected(tmp_path):
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    from test_word_lists import rewrite
    schema=project_schema(WORD_ACTION.json_schema(),['replace_image'])
    assert 'ReplaceImage' in schema['$defs'] and 'ImageFormat' not in schema['$defs']
    store,plugin,original,rev=placed(tmp_path)
    try:
        asset=replacement(tmp_path,store)
        def effects(parts):
            doc=ET.fromstring(parts['word/document.xml'])
            ET.SubElement(next(doc.iter(A+'blip')),A+'grayscl')
            parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,effects);rev=store.file_hash('report.docx')
        with pytest.raises(HarnessError,match='effects or alternate'):replace(plugin,rev,read(plugin,rev),asset)
        assert store.candidate('report.docx') is None
    finally:store.close()
