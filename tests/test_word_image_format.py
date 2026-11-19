import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.word.contracts import ImageFormat
from localoffice.errors import HarnessError
from test_word_images import setup,insert,W,WP
from test_word_lists import rewrite


def placed(root):
    store,plugin,original,rev,image,op=setup(root)
    result=insert(plugin,rev,op);store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
    return store,plugin,(root/'report.docx').read_bytes(),result['candidate_revision']


def read(plugin,rev):
    return plugin.read_objects(path='report.docx',revision=rev,ids=['image:1'],include_format=True)['objects'][0]


def edit(plugin,rev,target,props,key='format'):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'format','op':'format_image','target':target['id'],'expected_hash':target['hash'],'properties':props}],idempotency_key=key)


def test_resize_crop_alt_preserve_media_caption_other_content_and_exact_undo(tmp_path):
    store,plugin,original,rev=placed(tmp_path)
    try:
        target=read(plugin,rev);props={'width_inches':3,'crop_left_percent':10,'crop_right_percent':20,'alt_text':'Cropped blue image'}
        result=edit(plugin,rev,target,props)
        new=read(plugin,result['candidate_revision']);p=new['direct_format']['properties']
        assert p['width_inches']==3 and p['height_inches']==2
        assert p['crop_left_percent']==10 and p['crop_right_percent']==20 and p['crop_top_percent']==0
        assert p['alt_text']==props['alt_text']
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as updated:
            assert set(old.namelist())==set(updated.namelist())
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==updated.read(name)
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(updated.read('word/document.xml'))
            assert list(a.itertext())==list(b.itertext())
            for doc in (a,b):
                run=next(r for r in doc.iter('{'+W+'}r') if r.find('{'+W+'}drawing') is not None)
                run.remove(run.find('{'+W+'}drawing'))
            assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b))
        assert edit(plugin,rev,target,props)==result
        assert edit(plugin,result['candidate_revision'],new,props,'noop')['status']=='unchanged'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-format');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_partial_crop_checks_existing_edges_and_clears_without_cumulative_crop(tmp_path):
    store,plugin,original,rev=placed(tmp_path)
    try:
        first=edit(plugin,rev,read(plugin,rev),{'crop_right_percent':30.125})
        current=first['candidate_revision'];target=read(plugin,current)
        with pytest.raises(HarnessError,match='positive visible'):edit(plugin,current,target,{'width_inches':4,'crop_left_percent':70},'bad')
        assert store.candidate('report.docx')['candidate_hash']==current
        second=edit(plugin,current,target,{'crop_left_percent':10},'partial')
        properties=read(plugin,second['candidate_revision'])['direct_format']['properties']
        assert properties['crop_right_percent']==30.125 and properties['crop_left_percent']==10
        last=edit(plugin,second['candidate_revision'],read(plugin,second['candidate_revision']),{'crop_left_percent':0},'clear')
        assert read(plugin,last['candidate_revision'])['direct_format']['properties']['crop_left_percent']==0
    finally:store.close()


@pytest.mark.parametrize('props,expected',[({'height_inches':1.5},(2.25,1.5)),({'width_inches':4,'height_inches':1},(4,1))])
def test_image_frame_dimension_rules(tmp_path,props,expected):
    store,plugin,original,rev=placed(tmp_path)
    try:
        result=edit(plugin,rev,read(plugin,rev),props)
        p=read(plugin,result['candidate_revision'])['direct_format']['properties']
        assert (p['width_inches'],p['height_inches'])==expected
    finally:store.close()


@pytest.mark.parametrize('props',[{}, {'width_inches':0}, {'height_inches':23}, {'crop_left_percent':100}, {'crop_left_percent':50,'crop_right_percent':50}, {'crop_top_percent':0.0001}, {'crop_left_percent':-1}])
def test_image_format_bounds(props):
    with pytest.raises(ValidationError):ImageFormat(**props)


def test_linked_picture_is_read_only_and_wrong_or_stale_target_rejected(tmp_path):
    store,plugin,original,rev=placed(tmp_path)
    try:
        target=read(plugin,rev)
        with pytest.raises(HarnessError,match='stale_object'):edit(plugin,rev,{**target,'hash':'0'*64},{'width_inches':3})
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError,match='image target'):edit(plugin,rev,paragraph,{'width_inches':3})
        def change(parts):
            doc=ET.fromstring(parts['word/document.xml']);blip=doc.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
            rel='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
            value=blip.attrib.pop(rel+'embed');blip.set(rel+'link',value);parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx');target=read(plugin,rev)
        assert target['direct_format']['supported'] is False
        with pytest.raises(HarnessError,match='embedded'):edit(plugin,rev,target,{'width_inches':3})
        assert store.candidate('report.docx') is None
    finally:store.close()
