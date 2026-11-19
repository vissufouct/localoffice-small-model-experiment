import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.word.contracts import SectionLayout
from localoffice.errors import HarnessError
from test_word_tables import saved_table
from test_word_creation import W


def read_section(plugin,revision,id='section:1'):
    return plugin.read_objects(path='report.docx',revision=revision,ids=[id],include_format=True)['objects'][0]


def edit(plugin,revision,target,properties,key='layout'):
    return plugin.stage_batch(path='report.docx',revision=revision,idempotency_key=key,operations=[{
        'id':'layout','op':'set_section_layout','target':target['id'],'expected_hash':target['hash'],'properties':properties}])


def test_section_resize_preserves_content_parts_and_exact_undo(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        section=read_section(plugin,revision)
        assert section['direct_format']['orientation']=='landscape'
        properties={'width_twips':11906,'height_twips':16838,'left_twips':720,'right_twips':720}
        result=edit(plugin,revision,section,properties)
        current=result['candidate_revision'];after=read_section(plugin,current)
        assert after['direct_format']['orientation']=='portrait'
        assert after['direct_format']['properties']=={**section['direct_format']['properties'],**properties}
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(current))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            for doc in [a,b]:
                body=doc.find('{'+W+'}body');body.remove(body.find('{'+W+'}sectPr'))
            assert ET.tostring(a)==ET.tostring(b)
        assert (tmp_path/'report.docx').read_bytes()==original
        assert edit(plugin,revision,section,properties)==result
        assert edit(plugin,current,after,properties,'noop')['status']=='unchanged'
        with pytest.raises(HarnessError):edit(plugin,current,section,{'top_twips':800},'stale')
        store.apply(result['receipt_id'],expected_candidate=current,action='apply')
        undo=store.undo(result['receipt_id'],'undo-layout');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_section_geometry_rejects_impossible_area_and_wrong_target(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        section=read_section(plugin,revision)
        with pytest.raises(HarnessError,match='usable text area'):
            edit(plugin,revision,section,{'left_twips':14000,'right_twips':14000})
        paragraph=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:1'])['objects'][0]
        with pytest.raises(HarnessError,match='section target'):
            edit(plugin,revision,paragraph,{'left_twips':720},'wrong')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_multiple_sections_sparse_margin_keeps_other_geometry_and_settings(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        with zipfile.ZipFile(io.BytesIO(original)) as package:parts={n:package.read(n) for n in package.namelist()}
        doc=ET.fromstring(parts['word/document.xml']);body=doc.find('{'+W+'}body')
        final=body.find('{'+W+'}sectPr');copied=ET.fromstring(ET.tostring(final))
        ET.SubElement(copied,'{'+W+'}cols',{'{'+W+'}num':'2','{'+W+'}space':'720'})
        first=body.find('{'+W+'}p');props=first.find('{'+W+'}pPr');props.append(copied)
        parts['word/document.xml']=ET.tostring(doc)
        with zipfile.ZipFile(tmp_path/'report.docx','w') as package:
            for n,data in parts.items():package.writestr(n,data)
        revision=store.file_hash('report.docx');first=read_section(plugin,revision);last=read_section(plugin,revision,'section:2')
        result=edit(plugin,revision,first,{'top_twips':1200,'header_twips':600})
        after=read_section(plugin,result['candidate_revision'])
        assert after['direct_format']['properties']['top_twips']==1200
        assert after['direct_format']['orientation']=='landscape'
        assert read_section(plugin,result['candidate_revision'],'section:2')==last
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as package:
            changed=ET.fromstring(package.read('word/document.xml'))
            assert ET.canonicalize(ET.tostring(changed.find('.//{'+W+'}cols')))==ET.canonicalize(ET.tostring(copied.find('{'+W+'}cols')))
    finally:store.close()


@pytest.mark.parametrize('properties',[{}, {'width_twips':11906}, {'height_twips':16838}, {'left_twips':-1}, {'width_twips':99999,'height_twips':16838}, {'orientation':'portrait'}, {'top_twips':True}])
def test_layout_contract_requires_explicit_bounded_values(properties):
    with pytest.raises(ValidationError):SectionLayout(**properties)


@pytest.mark.parametrize('scope',['selection','artifact'])
def test_section_edits_respect_paragraph_annotation_scope(tmp_path,scope):
    from localoffice.annotations import annotate,bind_annotations
    from localoffice.chats import current_chat
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        paragraph=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:1'])['objects'][0]
        annotation=annotate(store,path='report.docx',revision=revision,scope=scope,
                           locator={'object_id':paragraph['id'],'object_hash':paragraph['hash']},comment='Change page margins')
        with store.transaction():
            store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('layout-task','Change margins','running','background','report.docx',1,1))
            store.db.execute('INSERT INTO task_chats VALUES(?,?)',('layout-task',current_chat(store)))
            bind_annotations(store,'layout-task','report.docx',[annotation])
        section=read_section(plugin,revision)
        args={'path':'report.docx','revision':revision,'operations':[{'id':'layout','op':'set_section_layout','target':section['id'],'expected_hash':section['hash'],'properties':{'left_twips':720}}], 'idempotency_key':'scoped','task_id':'layout-task'}
        if scope=='selection':
            with pytest.raises(HarnessError,match='outside'):plugin.stage_batch(**args)
            assert not store.candidate('report.docx')
        else:
            result=plugin.stage_batch(**args)
            assert store.db.execute("SELECT head_revision FROM task_scopes WHERE task_id='layout-task'").fetchone()[0]==result['candidate_revision']
    finally:store.close()


def test_unknown_existing_geometry_is_not_guessed(tmp_path):
    from test_word_worker import fixture_docx
    from localoffice.storage import ProjectStore
    from localoffice.word.plugin import WordPlugin
    fixture_docx(tmp_path/'report.docx');store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('report.docx');section=read_section(plugin,revision)
        assert section['direct_format']['properties']=={}
        with pytest.raises(HarnessError,match='unresolved'):edit(plugin,revision,section,{'left_twips':720})
        assert not store.candidate('report.docx')
        values={'width_twips':11906,'height_twips':16838,'left_twips':720,'right_twips':720,'top_twips':720,'bottom_twips':720,'header_twips':360,'footer_twips':360,'gutter_twips':0}
        result=edit(plugin,revision,section,values,'explicit')
        assert read_section(plugin,result['candidate_revision'])['direct_format']['properties']==values
    finally:store.close()
