import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.errors import HarnessError
from test_word_tables import saved_table
from test_word_styles import style_read
from test_word_lists import rewrite
from test_word_creation import W


def character_fixture(tmp_path,kind='character'):
    store,plugin,original,rev=saved_table(tmp_path)
    def change(parts):
        styles=ET.fromstring(parts['word/styles.xml'])
        base=ET.SubElement(styles,'{'+W+'}style',{'{'+W+'}type':kind,'{'+W+'}styleId':'BaseChar'})
        ET.SubElement(base,'{'+W+'}name',{'{'+W+'}val':'Base character'})
        props=ET.SubElement(base,'{'+W+'}rPr');ET.SubElement(props,'{'+W+'}i')
        style=ET.SubElement(styles,'{'+W+'}style',{'{'+W+'}type':kind,'{'+W+'}styleId':'Emphasis'})
        ET.SubElement(style,'{'+W+'}name',{'{'+W+'}val':'Report emphasis'})
        ET.SubElement(style,'{'+W+'}basedOn',{'{'+W+'}val':'BaseChar'})
        props=ET.SubElement(style,'{'+W+'}rPr')
        ET.SubElement(props,'{'+W+'}color',{'{'+W+'}val':'333333'})
        ET.SubElement(props,'{'+W+'}lang',{'{'+W+'}val':'pt-PT'})
        parts['word/styles.xml']=ET.tostring(styles)
        if kind=='character':
            doc=ET.fromstring(parts['word/document.xml'])
            para=doc.find('{'+W+'}body').findall('{'+W+'}p')[1]
            run=para.find('{'+W+'}r');props=run.find('{'+W+'}rPr')
            if props is None:props=ET.Element('{'+W+'}rPr');run.insert(0,props)
            props.insert(0,ET.Element('{'+W+'}rStyle',{'{'+W+'}val':'Emphasis'}))
            parts['word/document.xml']=ET.tostring(doc)
    rewrite(tmp_path,original,change)
    return store,plugin,(tmp_path/'report.docx').read_bytes(),store.file_hash('report.docx')


def edit(plugin,rev,target,properties,key='character',op='format_style_typography'):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':op,'target':target['id'],'expected_hash':target['hash'],'properties':properties}],idempotency_key=key)


def test_character_typography_preserves_all_other_properties_parts_and_assignments(tmp_path):
    store,plugin,original,rev=character_fixture(tmp_path)
    try:
        target=style_read(plugin,rev,'Emphasis')
        result=edit(plugin,rev,target,{'bold':True,'color':'17365D','size_points':14})
        updated=style_read(plugin,result['candidate_revision'],'Emphasis')
        props=updated['direct_format']['properties']
        assert props['type']=='character' and props['based_on']=='BaseChar'
        assert props['bold'] is True and props['color']=='17365D' and props['size_half_points']=='28'
        assert 'italic' not in props # remains inherited, never flattened
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert set(old.namelist())==set(new.namelist())
            for name in old.namelist():
                if name!='word/styles.xml':assert old.read(name)==new.read(name)
            before=ET.fromstring(old.read('word/styles.xml'));after=ET.fromstring(new.read('word/styles.xml'))
            style=next(s for s in after if s.get('{'+W+'}styleId')=='Emphasis');run=style.find('{'+W+'}rPr')
            for name in ['b','bCs','sz','szCs']:run.remove(run.find('{'+W+'}'+name))
            run.find('{'+W+'}color').set('{'+W+'}val','333333')
            assert ET.canonicalize(ET.tostring(before))==ET.canonicalize(ET.tostring(after))
        assert edit(plugin,rev,target,{'bold':True,'color':'17365D','size_points':14})==result
        assert edit(plugin,result['candidate_revision'],updated,{'bold':True,'color':'17365D','size_points':14},key='noop')['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-character');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('kind',['table','numbering'])
def test_typography_rejects_other_style_types(tmp_path,kind):
    store,plugin,original,rev=character_fixture(tmp_path,kind)
    try:
        with pytest.raises(HarnessError,match='paragraph or character'):
            edit(plugin,rev,style_read(plugin,rev,'Emphasis'),{'bold':True})
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_character_style_rejects_layout_and_stale_hash(tmp_path):
    from pydantic import ValidationError
    store,plugin,original,rev=character_fixture(tmp_path)
    try:
        target=style_read(plugin,rev,'Emphasis')
        with pytest.raises(ValidationError):edit(plugin,rev,target,{'spacing_after_twips':240})
        with pytest.raises(HarnessError,match='paragraph style'):edit(plugin,rev,target,{'spacing_after_twips':240},op='format_style',key='layout')
        with pytest.raises(HarnessError,match='stale_object'):edit(plugin,rev,{**target,'hash':'0'*64},{'bold':True},key='stale')
        assert store.candidate('report.docx') is None
    finally:store.close()
