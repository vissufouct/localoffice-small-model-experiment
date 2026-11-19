import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.errors import HarnessError
from test_word_tables import saved_table
from test_word_lists import rewrite
from test_word_creation import W


def style_read(plugin,revision,style='Body'):
    return plugin.read_objects(path='report.docx',revision=revision,ids=['style:'+style],include_format=True)['objects'][0]


def style_edit(plugin,revision,target,properties,key='style-edit',task_id=None):
    return plugin.stage_batch(path='report.docx',revision=revision,operations=[{'id':'style','op':'format_style','target':target['id'],'expected_hash':target['hash'],'properties':properties}],idempotency_key=key,task_id=task_id)


@pytest.mark.parametrize('points,half',[(6,12),(14,28),(14.5,29),(144,288)])
def test_style_font_points_convert_exactly_and_replay_as_legacy_noop(tmp_path,points,half):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        result=style_edit(plugin,rev,target,{'size_points':points})
        updated=style_read(plugin,result['candidate_revision'])
        assert updated['direct_format']['properties']['size_half_points']==str(half)
        assert updated['direct_format']['properties']['spacing_after_twips']==target['direct_format']['properties']['spacing_after_twips']
        assert style_edit(plugin,result['candidate_revision'],updated,{'size_half_points':half},key='legacy')['status']=='unchanged'
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert old.read('word/document.xml')==new.read('word/document.xml')
    finally:store.close()


@pytest.mark.parametrize('properties',[{'size_points':5.5},{'size_points':144.5},{'size_points':14.25},{'size_points':float('nan')},{'size_points':14,'size_half_points':28}])
def test_point_font_size_rejects_out_of_range_fractional_or_ambiguous_values(properties):
    from localoffice.word.contracts import ParagraphFormat
    from pydantic import ValidationError
    with pytest.raises(ValidationError):ParagraphFormat(**properties)


def test_paragraph_point_size_changes_only_target_runs(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'font','op':'format_paragraph','target':target['id'],'expected_hash':target['hash'],'properties':{'size_points':13.5}}],idempotency_key='points')
        updated=plugin.read_objects(path='report.docx',revision=result['candidate_revision'],ids=[target['id']],include_format=True)['objects'][0]
        assert updated['text']==target['text'] and all(r['size_half_points']=='27' for r in updated['direct_format']['runs'])
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert old.read('word/styles.xml')==new.read('word/styles.xml')
    finally:store.close()


def test_typography_only_style_tool_preserves_layout_and_rejects_layout_fields(tmp_path):
    from localoffice.word.contracts import StageBatch
    from pydantic import ValidationError
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        op={'id':'font','op':'format_style_typography','target':target['id'],'expected_hash':target['hash'],'properties':{'font':'Arial','size_points':14,'bold':True,'color':'17365D'}}
        with pytest.raises(ValidationError):
            StageBatch(path='report.docx',revision=rev,operations=[{**op,'properties':{**op['properties'],'spacing_after_twips':0}}])
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='typography')
        updated=style_read(plugin,result['candidate_revision'])['direct_format']['properties']
        assert updated['size_half_points']=='28' and updated['bold'] is True
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/styles.xml'));b=ET.fromstring(new.read('word/styles.xml'))
            def body_layout(root):return next(s for s in root if s.get('{'+W+'}styleId')=='Body').find('{'+W+'}pPr')
            assert ET.canonicalize(ET.tostring(body_layout(a)))==ET.canonicalize(ET.tostring(body_layout(b)))
    finally:store.close()


def test_style_definition_sparse_edit_preserves_content_other_styles_and_exact_undo(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        properties={'font':'Calibri','size_half_points':26,'color':'123456','bold':True,'spacing_after_twips':240}
        result=style_edit(plugin,rev,target,properties)
        updated=style_read(plugin,result['candidate_revision'])
        for key,value in properties.items():
            assert str(updated['direct_format']['properties'][key])==str(value)
        assert result['results'][0]['target']=='style:Body'
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/styles.xml'));b=ET.fromstring(new.read('word/styles.xml'))
            for sa,sb in zip(a,b):
                if sa.get('{'+W+'}styleId')!='Body':assert ET.canonicalize(ET.tostring(sa))==ET.canonicalize(ET.tostring(sb))
        assert (tmp_path/'report.docx').read_bytes()==original
        assert style_edit(plugin,rev,target,properties)==result
        assert style_edit(plugin,result['candidate_revision'],updated,properties,key='noop')['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'style-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_style_definition_failure_is_atomic_and_requires_style_target(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        with pytest.raises(HarnessError,match='stale_object'):
            style_edit(plugin,rev,{**target,'hash':'0'*64},{'bold':True})
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError,match='style target'):
            style_edit(plugin,rev,paragraph,{'bold':True})
        with pytest.raises(HarnessError):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[
                {'id':'first','op':'format_style','target':target['id'],'expected_hash':target['hash'],'properties':{'bold':True}},
                {'id':'bad','op':'format_style','target':paragraph['id'],'expected_hash':paragraph['hash'],'properties':{'bold':True}}],idempotency_key='atomic')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_style_and_content_batch_changes_only_two_owned_parts(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=style_read(plugin,rev)
        paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[
            {'id':'definition','op':'format_style','target':target['id'],'expected_hash':target['hash'],'properties':{'italic':True}},
            {'id':'content','op':'replace_text','target':paragraph['id'],'expected_hash':paragraph['hash'],'start':0,'end':len(paragraph['text']),'text':'Revised introduction.'}],idempotency_key='mixed')
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            changed={name for name in old.namelist() if old.read(name)!=new.read(name)}
            assert changed=={'word/document.xml','word/styles.xml'}
        current=result['candidate_revision']
        assert style_read(plugin,current)['direct_format']['properties']['italic'] is True
        assert plugin.read_objects(path='report.docx',revision=current,ids=['paragraph:2'])['objects'][0]['text']=='Revised introduction.'
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.asyncio
async def test_paragraph_annotation_cannot_change_shared_style_definition(tmp_path):
    from localoffice.annotations import annotate
    from localoffice.runtime import TaskRunner
    import json
    store,plugin,original,rev=saved_table(tmp_path)
    paragraph=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
    annotation=annotate(store,path='report.docx',revision=rev,scope='selection',locator={'object_id':paragraph['id'],'object_hash':paragraph['hash']},comment='Bold this paragraph only.')
    target=style_read(plugin,rev)
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 400
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':json.dumps({'tool':'word.stage_batch','path':'report.docx','revision':rev,'operations':[{'id':'global','op':'format_style','target':target['id'],'expected_hash':target['hash'],'properties':{'bold':True}}]})}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        await runner.start('Apply annotation',artifact='report.docx',annotation_ids=[annotation]);await runner.job
        assert any(e['type']=='tool_error' and e['code']=='scope_violation' for e in runner.events)
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()


@pytest.mark.parametrize('kind',['paragraph','character'])
def test_style_definition_preserves_links_and_unrequested_properties(tmp_path,kind):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        def change(parts):
            root=ET.fromstring(parts['word/styles.xml']);style=next(s for s in root if s.get('{'+W+'}styleId')=='Body')
            style.set('{'+W+'}type',kind)
            # Style metadata precedes paragraph/run properties in OOXML.
            style.insert(1,ET.Element('{'+W+'}basedOn',{'{'+W+'}val':'Normal'}))
            r=style.find('{'+W+'}rPr');ET.SubElement(r,'{'+W+'}lang',{'{'+W+'}val':'pt-PT'})
            parts['word/styles.xml']=ET.tostring(root)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx');target=style_read(plugin,rev)
        if kind=='character':
            with pytest.raises(HarnessError,match='paragraph style'):style_edit(plugin,rev,target,{'italic':True})
            assert store.candidate('report.docx') is None
            return
        result=style_edit(plugin,rev,target,{'italic':True})
        updated=style_read(plugin,result['candidate_revision'])['direct_format']['properties']
        assert updated['based_on']=='Normal' and updated['italic'] is True
        assert updated['size_half_points']==target['direct_format']['properties']['size_half_points']
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as package:
            root=ET.fromstring(package.read('word/styles.xml'));style=next(s for s in root if s.get('{'+W+'}styleId')=='Body')
            assert style.find('.//{'+W+'}lang').get('{'+W+'}val')=='pt-PT'
    finally:store.close()


def test_style_discovery_read_and_assignment_preserve_definitions(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        page=plugin.list_objects(path='report.docx',revision=rev,kind='style',limit=1)
        assert len(page['objects'])==1 and page['next_cursor']
        rest=plugin.list_objects(path='report.docx',revision=rev,kind='style',cursor=page['next_cursor'])
        assert {o['id'] for o in page['objects']+rest['objects']}=={'style:Normal','style:Title','style:Body'}
        read=plugin.read_objects(path='report.docx',revision=rev,ids=['style:Title'],include_format=True)['objects'][0]
        props=read['direct_format']['properties']
        assert props['style_id']=='Title' and props['type']=='paragraph'
        assert props['font']=='Arial' and props['size_half_points']=='36' and props['bold'] is True
        assert props['color']=='17365D' and props['outline_level']=='0'
        target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[{'id':'style','op':'set_style','target':target['id'],'expected_hash':target['hash'],'style_id':props['style_id']}],idempotency_key='assign')
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert old.read('word/styles.xml')==new.read('word/styles.xml')
        assert (tmp_path/'report.docx').read_bytes()==original
        with pytest.raises(HarnessError):
            plugin.list_objects(path='report.docx',revision=result['candidate_revision'],kind='style',cursor=page['next_cursor'])
    finally:store.close()


def test_style_read_bounds_and_reports_direct_inheritance(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        def change(parts):
            root=ET.fromstring(parts['word/styles.xml']);style=next(s for s in root if s.get('{'+W+'}styleId')=='Body')
            style.find('{'+W+'}name').set('{'+W+'}val','X'*500)
            ET.SubElement(style,'{'+W+'}basedOn',{'{'+W+'}val':'Title'})
            parts['word/styles.xml']=ET.tostring(root)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx')
        listing=plugin.list_objects(path='report.docx',revision=rev,kind='style',preview_chars=10)
        assert len(next(o for o in listing['objects'] if o['id']=='style:Body')['preview'])==10
        obj=plugin.read_objects(path='report.docx',revision=rev,ids=['style:Body'],max_chars=12,include_format=True)['objects'][0]
        assert len(obj['text'])==12 and obj['next_offset']==12
        fmt=obj['direct_format'];assert fmt['values_truncated'] is True
        assert len(fmt['properties']['name'])==160 and fmt['properties']['based_on']=='Title'
        assert 'not resolved' in fmt['interpretation']
        assert store.candidate('report.docx') is None
    finally:store.close()
