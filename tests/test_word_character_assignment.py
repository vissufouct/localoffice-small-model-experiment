import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import CreateCharacterStyle,SetCharacterStyle
from test_word_character_styles import character_fixture
from test_word_styles import style_read
from test_word_text_format import fixture,get,characters,N
from test_word_lists import rewrite


def create(plugin,rev,target,**changes):
    op={'op':'create_character_style','target':target['id'],'expected_hash':target['hash'],'style_id':'Callout','name':'Report callout','properties':{'bold':True,'color':'17365D'}}
    op.update(changes)
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='create-character')


def assign(plugin,rev,target,style='Callout',key='assign',task_id=None):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'set_character_style','target':target['id'],'expected_hash':target['hash'],'style_id':style,'start':1,'end':12}],idempotency_key=key,task_id=task_id)


def test_character_creation_inheritance_preservation_replay_and_exact_undo(tmp_path):
    store,plugin,original,rev=character_fixture(tmp_path)
    try:
        target=style_read(plugin,rev,'Emphasis');result=create(plugin,rev,target)
        props=style_read(plugin,result['candidate_revision'],'Callout')['direct_format']['properties']
        assert props['type']=='character' and props['based_on']=='Emphasis'
        assert props['bold'] is True and props['color']=='17365D' and 'italic' not in props
        assert style_read(plugin,result['candidate_revision'],'Emphasis')['hash']==target['hash']
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name)
            before=ET.fromstring(old.read('word/styles.xml'));after=ET.fromstring(new.read('word/styles.xml'))
            after.remove(next(s for s in after if s.get(N+'styleId')=='Callout'))
            assert ET.canonicalize(ET.tostring(before))==ET.canonicalize(ET.tostring(after))
        assert create(plugin,rev,target)==result
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def assignment_fixture(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    def change(parts):
        styles=ET.fromstring(parts['word/styles.xml'])
        style=ET.SubElement(styles,N+'style',{N+'type':'character',N+'styleId':'Callout'})
        ET.SubElement(style,N+'name',{N+'val':'Report callout'})
        props=ET.SubElement(style,N+'rPr');ET.SubElement(props,N+'b');ET.SubElement(props,N+'color',{N+'val':'17365D'})
        parts['word/styles.xml']=ET.tostring(styles)
    rewrite(tmp_path,original,change)
    return store,plugin,(tmp_path/'report.docx').read_bytes(),store.file_hash('report.docx')


def test_assignment_unicode_hyperlink_overrides_noop_and_undo(tmp_path):
    store,plugin,original,rev=assignment_fixture(tmp_path)
    try:
        target=get(plugin,rev);result=assign(plugin,rev,target);data=store.blob(result['candidate_revision'])
        a,before=characters(original);b,after=characters(data)
        assert get(plugin,result['candidate_revision'])['text']==target['text']
        for i,(old,new) in enumerate(zip(before,after,strict=True)):
            assert old[0]==new[0] and old[2]==new[2]
            assert new[1]==({**old[1],N+'rStyle':{N+'val':'Callout'}} if 1<=i<12 else old[1])
        for tag in ['pPr','bookmarkStart','bookmarkEnd']:
            assert ET.canonicalize(ET.tostring(a.find(N+tag)))==ET.canonicalize(ET.tostring(b.find(N+tag)))
        assert a.find(N+'hyperlink').attrib==b.find(N+'hyperlink').attrib
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(data)) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            beforedoc=ET.fromstring(old.read('word/document.xml'));afterdoc=ET.fromstring(new.read('word/document.xml'))
            for doc in (beforedoc,afterdoc):
                body=doc.find(N+'body');body.remove(body.findall(N+'p')[1])
            assert ET.canonicalize(ET.tostring(beforedoc))==ET.canonicalize(ET.tostring(afterdoc))
        assert assign(plugin,rev,target)==result
        assert assign(plugin,result['candidate_revision'],get(plugin,result['candidate_revision']),key='noop')['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_character_operations_reject_wrong_types_stale_evidence_and_layout(tmp_path):
    store,plugin,original,rev=assignment_fixture(tmp_path)
    try:
        with pytest.raises(HarnessError,match='character style'):create(plugin,rev,style_read(plugin,rev))
        with pytest.raises(HarnessError,match='character style'):assign(plugin,rev,get(plugin,rev),style='Body')
        with pytest.raises(HarnessError,match='stale_object'):assign(plugin,rev,{**get(plugin,rev),'hash':'0'*64})
        with pytest.raises(ValidationError):CreateCharacterStyle(target='style:Body',expected_hash='0'*64,style_id='X',name='X',properties={'spacing_after_twips':120})
        with pytest.raises(ValidationError):SetCharacterStyle(target='paragraph:2',expected_hash='0'*64,style_id='X',start=3,end=3)
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_character_assignment_respects_paragraph_scope_and_lazy_loading(tmp_path):
    from test_word_progress import task
    from localoffice.word.guidance import load,project_schema
    from localoffice.runtime import WORD_ACTION
    from localoffice.storage import canonical
    from localoffice.annotations import validate_word_scope
    store,plugin,original,rev=assignment_fixture(tmp_path);task(store)
    try:
        with store.transaction():store.db.execute('INSERT INTO task_scopes VALUES(?,?,?,?,?)',('task','report.docx',rev,rev,canonical({'format':'docx','objects':['paragraph:1']})))
        load(store,'task',['set_character_style'])
        with pytest.raises(HarnessError,match='outside the annotated selection'):assign(plugin,rev,get(plugin,rev),task_id='task')
        assert store.candidate('report.docx') is None
        with store.transaction():store.db.execute('UPDATE task_scopes SET ranges=? WHERE task_id=?',(canonical({'format':'docx','objects':['paragraph:2']}),'task'))
        creation=CreateCharacterStyle(target='style:Callout',expected_hash=style_read(plugin,rev,'Callout')['hash'],style_id='New',name='New')
        with pytest.raises(HarnessError,match='whole-artifact scope'):validate_word_scope(store,'task','report.docx',rev,[creation])
        result=assign(plugin,rev,get(plugin,rev),task_id='task')
        assert result['status']=='validated' and (tmp_path/'report.docx').read_bytes()==original
        schema=project_schema(WORD_ACTION.json_schema(),['set_character_style'])
        assert 'SetCharacterStyle' in schema['$defs'] and 'CreateCharacterStyle' not in schema['$defs']
        schema=project_schema(WORD_ACTION.json_schema(),['create_character_style'])
        assert 'CreateCharacterStyle' in schema['$defs'] and 'ParagraphFormat' not in schema['$defs']
    finally:store.close()


@pytest.mark.parametrize('properties',[None,{'bold':True,'color':'17365D','size_points':14}])
def test_independent_character_style_without_character_base(tmp_path,properties):
    from test_word_tables import saved_table
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        with zipfile.ZipFile(io.BytesIO(original)) as package:
            styles=ET.fromstring(package.read('word/styles.xml'))
            assert not any(s.get(N+'type')=='character' for s in styles)
        anchor=style_read(plugin,rev)
        result=create(plugin,rev,anchor,inherit_base=False,properties=properties)
        current=result['candidate_revision'];props=style_read(plugin,current,'Callout')['direct_format']['properties']
        assert props['type']=='character' and 'based_on' not in props
        assert not props['default'] and props['name']=='Report callout'
        assert style_read(plugin,current)['hash']==anchor['hash']
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(current))) as new:
            assert old.namelist()==new.namelist()
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name)
            before=ET.fromstring(old.read('word/styles.xml'));after=ET.fromstring(new.read('word/styles.xml'))
            created=next(s for s in after if s.get(N+'styleId')=='Callout')
            assert created.find(N+'basedOn') is None and created.find(N+'pPr') is None
            if properties is None:assert created.find(N+'rPr') is None
            else:
                assert props['bold'] is True and props['size_half_points']=='28' and props['color']=='17365D'
            after.remove(created)
            assert ET.canonicalize(ET.tostring(before))==ET.canonicalize(ET.tostring(after))
        assert create(plugin,rev,anchor,inherit_base=False,properties=properties)==result
        repeat={'op':'create_character_style','target':anchor['id'],'expected_hash':anchor['hash'],'style_id':'Callout','name':'Report callout','inherit_base':False,'properties':properties}
        noop=plugin.stage_batch(path='report.docx',revision=current,operations=[repeat],idempotency_key='noop-independent')
        assert noop['status']=='unchanged'
        changed={**repeat,'name':'Different name'}
        with pytest.raises(HarnessError,match='already exists'):plugin.stage_batch(path='report.docx',revision=current,operations=[changed],idempotency_key='collision')
        assert store.candidate('report.docx')['candidate_hash']==current
        paragraph=get(plugin,current)
        applied=assign(plugin,current,paragraph)
        assert get(plugin,applied['candidate_revision'])['text']==paragraph['text']
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(applied['receipt_id'],expected_candidate=applied['candidate_revision'],action='apply')
        undo=store.undo(applied['receipt_id'],'undo-independent');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()
