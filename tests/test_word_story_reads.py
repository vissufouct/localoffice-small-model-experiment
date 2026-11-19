import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.errors import HarnessError
from test_word_page_bands import band_design
from test_word_creation import W
from test_word_lists import rewrite
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def fixture(root):
    store=ProjectStore(root);plugin=WordPlugin(store)
    result=plugin.create(**band_design(),idempotency_key='fixture')
    store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='save')
    return store,plugin,(root/'report.docx').read_bytes(),result['candidate_revision']


def test_bounded_story_reads_report_cached_fields_and_never_mutate(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        header=plugin.list_objects(path='report.docx',revision=rev,kind='header',preview_chars=7)['objects'][0]
        footer=plugin.list_objects(path='report.docx',revision=rev,kind='footer')['objects'][0]
        assert header['id']=='header:1' and header['preview']=='Quarter'
        read=plugin.read_objects(path='report.docx',revision=rev,ids=[header['id'],footer['id']])['objects']
        assert read[0]['text']=='Quarterly report'
        assert read[0]['story']['explicit_section_references']==[{'section':'section:1','type':'default'}]
        assert read[0]['story']['read_only'] and not read[0]['story']['contains_fields']
        assert read[1]['text']=='Page 1 of 1' # cache, not a current pagination result
        assert read[1]['story']['contains_fields'] and 'cached' in read[1]['story']['interpretation']
        chunk=plugin.read_objects(path='report.docx',revision=rev,ids=[header['id']],max_chars=4)['objects'][0]
        assert chunk['text']=='Quar' and chunk['next_offset']==4
        rest=plugin.read_objects(path='report.docx',revision=rev,ids=[header['id']],offset=4,max_chars=4)['objects'][0]
        assert rest['text']=='terl' and rest['hash']==header['hash']
        with pytest.raises(HarnessError,match='Unsupported story edit'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'replace_text','target':header['id'],'expected_hash':header['hash'],'start':0,'end':1,'text':'X'}],idempotency_key='blocked')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
        assert plugin.find_text(path='report.docx',revision=rev,query='Quarterly')['matches']==[] # declared main-story search unchanged
    finally:store.close()


def test_shared_header_lists_explicit_first_and_default_section_references(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        def change(parts):
            doc=ET.fromstring(parts['word/document.xml']);body=doc.find('{'+W+'}body');section=body.find('{'+W+'}sectPr')
            header=section.find('{'+W+'}headerReference');rid=header.get('{'+R+'}id')
            section.insert(1,ET.Element('{'+W+'}headerReference',{'{'+W+'}type':'first','{'+R+'}id':rid}))
            p=ET.Element('{'+W+'}p');props=ET.SubElement(p,'{'+W+'}pPr');first=ET.SubElement(props,'{'+W+'}sectPr')
            ET.SubElement(first,'{'+W+'}headerReference',{'{'+W+'}type':'default','{'+R+'}id':rid})
            body.insert(0,p);parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx')
        listed=plugin.list_objects(path='report.docx',revision=rev,kind='header')['objects']
        assert len(listed)==1 # one shared part, not one copy per reference
        info=plugin.read_objects(path='report.docx',revision=rev,ids=[listed[0]['id']])['objects'][0]['story']
        assert info['explicit_section_references']==[
            {'section':'section:1','type':'default'}, {'section':'section:2','type':'default'}, {'section':'section:2','type':'first'}]
        assert not info['references_truncated']
        with pytest.raises(HarnessError) as error:
            plugin.read_objects(path='report.docx',revision='0'*64,ids=['header:1'])
        assert error.value.code=='stale_revision'
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_story_reference_metadata_is_bounded(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        def change(parts):
            doc=ET.fromstring(parts['word/document.xml']);body=doc.find('{'+W+'}body')
            rid=body.find('{'+W+'}sectPr/{'+W+'}headerReference').get('{'+R+'}id')
            for _ in range(20):
                p=ET.Element('{'+W+'}p');props=ET.SubElement(p,'{'+W+'}pPr');section=ET.SubElement(props,'{'+W+'}sectPr')
                ET.SubElement(section,'{'+W+'}headerReference',{'{'+W+'}type':'default','{'+R+'}id':rid});body.insert(0,p)
            parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx')
        read=plugin.read_objects(path='report.docx',revision=rev,ids=['header:1'],max_chars=1)['objects'][0]
        assert read['text']=='Q' and read['next_offset']==1
        assert read['story']['references_truncated']
        assert len(read['story']['explicit_section_references'])==16
    finally:store.close()


def test_header_content_control_text_is_not_silently_omitted(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        def change(parts):
            name=next(n for n in parts if n.startswith('word/header') and n.endswith('.xml'))
            header=ET.fromstring(parts[name]);paragraph=header.find('{'+W+'}p');header.remove(paragraph)
            control=ET.SubElement(header,'{'+W+'}sdt');content=ET.SubElement(control,'{'+W+'}sdtContent');content.append(paragraph)
            parts[name]=ET.tostring(header)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx')
        read=plugin.read_objects(path='report.docx',revision=rev,ids=['header:1'])['objects'][0]
        assert read['text']=='Quarterly report'
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_story_descendants_are_parent_scoped_and_keep_body_ids(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        before=plugin.list_objects(path='report.docx',revision=rev,kind='paragraph')['objects']
        assert before and all(o['id'].count(':')==1 for o in before)
        header=plugin.list_objects(path='report.docx',revision=rev,kind='header')['objects'][0]
        children=plugin.list_objects(path='report.docx',revision=rev,parent=header['id'],kind='paragraph')['objects']
        assert len(children)==1 and children[0]['id']=='paragraph:header:1:1'
        read=plugin.read_objects(path='report.docx',revision=rev,ids=[children[0]['id']],include_format=True)['objects'][0]
        assert read['text']=='Quarterly report'
        assert read['story']['root_id']==header['id'] and not read['story']['read_only']
        assert 'format_text' in read['story']['editable_operations']
        assert read['direct_format']['properties']['alignment']=='right'
        assert plugin.list_objects(path='report.docx',revision=rev,kind='paragraph')['objects']==before
        with pytest.raises(HarnessError,match='Unsupported story edit'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'insert_paragraphs','target':read['id'],'expected_hash':read['hash'],'position':'after','paragraphs':[{'text':'Not supported here'}]}],idempotency_key='no-story-write')
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_story_nested_table_and_paragraph_pagination(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        def change(parts):
            name=next(n for n in parts if n.startswith('word/header') and n.endswith('.xml'))
            header=ET.fromstring(parts[name])
            table=ET.SubElement(header,'{'+W+'}tbl');row=ET.SubElement(table,'{'+W+'}tr');cell=ET.SubElement(row,'{'+W+'}tc')
            for value in ['Nested one','Nested two']:
                p=ET.SubElement(cell,'{'+W+'}p');run=ET.SubElement(p,'{'+W+'}r');ET.SubElement(run,'{'+W+'}t').text=value
            parts[name]=ET.tostring(header)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx')
        table=plugin.list_objects(path='report.docx',revision=rev,parent='header:1',kind='table')['objects'][0]
        row=plugin.list_objects(path='report.docx',revision=rev,parent=table['id'],kind='row')['objects'][0]
        cell=plugin.list_objects(path='report.docx',revision=rev,parent=row['id'],kind='cell')['objects'][0]
        first=plugin.list_objects(path='report.docx',revision=rev,parent=cell['id'],kind='paragraph',limit=1)
        second=plugin.list_objects(path='report.docx',revision=rev,parent=cell['id'],kind='paragraph',limit=1,cursor=first['next_cursor'])
        assert [o['preview'] for o in first['objects']+second['objects']]==['Nested one','Nested two']
        assert second['next_cursor'] is None
        for parent,kind in [('header:1','paragraph'),(cell['id'],'cell')]:
            with pytest.raises(HarnessError,match='stale_cursor'):
                plugin.list_objects(path='report.docx',revision=rev,parent=parent,kind=kind,cursor=first['next_cursor'])
        roots=plugin.read_objects(path='report.docx',revision=rev,ids=[cell['id']])['objects'][0]
        assert roots['text']=='Nested one\nNested two' and roots['story']['read_only']
    finally:store.close()
