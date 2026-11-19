import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.word.contracts import StageBatch,LoadTools
from localoffice.word.guidance import project_schema
from localoffice.runtime import WORD_ACTION
from localoffice.errors import HarnessError
from test_word_tables import saved_table,get
from test_word_creation import W
from test_word_table_annotations import bind
from test_word_lists import rewrite


def edit(plugin,rev,props,target='cell:1',expected_hash=None,**extra):
    obj={'hash':expected_hash} if expected_hash else get(plugin,rev,target)
    return plugin.stage_batch(path='report.docx',revision=rev,
        operations=[{'op':'format_cell','target':target,'expected_hash':obj['hash'],'properties':props}],
        idempotency_key=rev+target,**extra)


def test_cell_format_preserves_content_layout_and_scope_with_exact_undo(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        bind(store,plugin,rev,['cell:1'])
        props={'fill':'DBEAFE','vertical_alignment':'center','padding_top_twips':120,'padding_left_twips':180}
        original_hash=get(plugin,rev,'cell:1')['hash']
        result=edit(plugin,rev,props,task_id='table-task');head=result['candidate_revision']
        assert edit(plugin,rev,props,expected_hash=original_hash,task_id='table-task')==result
        direct=plugin.read_objects(path='report.docx',revision=head,ids=['cell:1'],include_format=True)['objects'][0]['direct_format']
        assert direct['properties']['fill']=='DBEAFE' and direct['properties']['vertical_alignment']=='center'
        assert direct['structure']['grid_span']==1
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(head))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            cells_a=a.findall('.//{'+W+'}tc');cells_b=b.findall('.//{'+W+'}tc')
            assert [ET.tostring(c) for c in cells_a[1:]]==[ET.tostring(c) for c in cells_b[1:]]
            pa=cells_a[0].find('{'+W+'}tcPr');pb=cells_b[0].find('{'+W+'}tcPr')
            for name in ['shd','vAlign','tcMar']:
                e=pb.find('{'+W+'}'+name)
                if e is not None:pb.remove(e)
            assert ET.tostring(a)==ET.tostring(b)
        same=edit(plugin,head,props,task_id='table-task');assert same['status']=='unchanged'
        with pytest.raises(HarnessError,match='outside'):
            edit(plugin,head,{'fill':'FFFFFF'},target='cell:2',task_id='table-task')
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=head,action='apply')
        undo=store.undo(result['receipt_id'],'undo-cell-format');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_cell_alignment_preserves_theme_pattern_and_merges(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        def change(parts):
            doc=ET.fromstring(parts['word/document.xml']);cell=doc.find('.//{'+W+'}tc');props=cell.find('{'+W+'}tcPr')
            ET.SubElement(props,'{'+W+'}vMerge',{'{'+W+'}val':'restart'})
            ET.SubElement(props,'{'+W+'}shd',{'{'+W+'}val':'pct20','{'+W+'}fill':'AABBCC','{'+W+'}themeFill':'accent1'})
            # Continue the merge in the cell directly underneath.
            lower=doc.findall('.//{'+W+'}tr')[1].find('{'+W+'}tc/{'+W+'}tcPr')
            ET.SubElement(lower,'{'+W+'}vMerge')
            parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx')
        result=edit(plugin,rev,{'vertical_alignment':'bottom'});head=result['candidate_revision']
        info=plugin.read_objects(path='report.docx',revision=head,ids=['cell:1'],include_format=True)['objects'][0]['direct_format']
        assert info['properties']['theme_fill']=='accent1' and info['properties']['shading_pattern']=='pct20'
        assert info['structure']['vertical_merge']=='restart'
        result=edit(plugin,head,{'fill':'auto'})
        info=plugin.read_objects(path='report.docx',revision=result['candidate_revision'],ids=['cell:1'],include_format=True)['objects'][0]['direct_format']
        assert info['properties']['fill']=='auto' and info['properties']['shading_pattern']=='clear'
        assert info['properties']['theme_fill'] is None and info['properties']['vertical_alignment']=='bottom'
        assert info['structure']['vertical_merge']=='restart'
    finally:store.close()


@pytest.mark.parametrize('props',[{}, {'fill':'blue'}, {'vertical_alignment':'middle'}, {'padding_top_twips':2881}])
def test_cell_format_invalid_properties_and_lazy_schema(tmp_path,props):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        with pytest.raises(ValidationError):
            StageBatch(path='report.docx',revision=rev,operations=[{'op':'format_cell','target':'cell:1','expected_hash':'0'*64,'properties':props}])
        assert LoadTools(operations=['format_cell']).operations==['format_cell']
        assert 'FormatCell' not in project_schema(WORD_ACTION.json_schema(),[])['$defs']
        assert 'FormatCell' in project_schema(WORD_ACTION.json_schema(),['format_cell'])['$defs']
        assert store.candidate('report.docx') is None
    finally:store.close()
