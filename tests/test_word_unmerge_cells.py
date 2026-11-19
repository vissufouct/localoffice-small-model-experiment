import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.word.contracts import StageBatch,LoadTools
from localoffice.errors import HarnessError
from test_word_merge_cells import fixture,op as merge_op,xml
from test_word_creation import W
from test_word_table_annotations import bind
from test_word_lists import rewrite


def merged(root,height=2,width=2):
    store,plugin,original,rev=fixture(root)
    made=plugin.stage_batch(path='report.docx',revision=rev,operations=[merge_op(plugin,rev,row_count=height,column_count=width)],idempotency_key='fixture-merge')
    store.apply(made['receipt_id'],expected_candidate=made['candidate_revision'],action='apply')
    return store,plugin,(root/'report.docx').read_bytes(),made['candidate_revision']


def op(plugin,rev,target='cell:1'):
    obj=plugin.read_objects(path='report.docx',revision=rev,ids=[target])['objects'][0]
    return {'op':'unmerge_cells','target':target,'expected_hash':obj['hash']}


@pytest.mark.parametrize('height,width',[(1,2),(2,1),(2,2)])
def test_unmerge_preserves_current_content_and_grid_and_undo_restores_exact_merge(tmp_path,height,width):
    store,plugin,original,rev=merged(tmp_path,height,width)
    try:
        args=dict(path='report.docx',revision=rev,operations=[op(plugin,rev)],idempotency_key='split')
        result=plugin.stage_batch(**args);assert plugin.stage_batch(**args)==result;head=result['candidate_revision']
        a=xml(original);b=xml(store.blob(head));old_rows=a.findall('.//{'+W+'}tr');new_rows=b.findall('.//{'+W+'}tr')
        assert all(len(r.findall('{'+W+'}tc'))==3 for r in new_rows)
        assert not b.findall('.//{'+W+'}gridSpan') and not b.findall('.//{'+W+'}vMerge')
        for r in range(height):
            old_cell=old_rows[r].find('{'+W+'}tc');new_cells=new_rows[r].findall('{'+W+'}tc')
            assert [ET.tostring(p) for p in old_cell.findall('{'+W+'}p')]==[ET.tostring(p) for p in new_cells[0].findall('{'+W+'}p')]
            assert all(''.join(c.itertext())=='' for c in new_cells[1:width])
        # Existing nonselected cells keep their complete XML, not only text.
        for r,row in enumerate(old_rows):
            for j,cell in enumerate(row.findall('{'+W+'}tc')):
                if r<height and j==0:continue
                matches=[c for c in b.findall('.//{'+W+'}tc') if ''.join(c.itertext())==''.join(cell.itertext())]
                assert len(matches)==1 and ET.tostring(matches[0])==ET.tostring(cell)
        assert ET.tostring(a.find('.//{'+W+'}tblGrid'))==ET.tostring(b.find('.//{'+W+'}tblGrid'))
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(head))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
        assert (tmp_path/'report.docx').read_bytes()==original
        same=plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head)],idempotency_key='noop')
        assert same['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=head,action='apply');undo=store.undo(result['receipt_id'],'undo-split')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply');assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_unmerge_rejects_continuation_and_scoped_structural_change(tmp_path):
    store,plugin,original,rev=merged(tmp_path)
    try:
        cells=plugin.list_objects(path='report.docx',revision=rev,kind='cell')['objects']
        continuation=cells[2] # row 2 first physical cell after horizontal collapse
        with pytest.raises(HarnessError,match='top-left'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev,continuation['id'])],idempotency_key='wrong')
        bind(store,plugin,rev,['cell:1'])
        with pytest.raises(HarnessError,match='whole-artifact'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev)],idempotency_key='scope',task_id='table-task')
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('malformed',['span','width','legacy','tracked'])
def test_unmerge_rejects_ambiguous_geometry_without_mutation(tmp_path,malformed):
    store,plugin,original,rev=merged(tmp_path)
    try:
        def change(parts):
            doc=ET.fromstring(parts['word/document.xml']);rows=doc.findall('.//{'+W+'}tr')
            if malformed=='span':
                cells=rows[1].findall('{'+W+'}tc');cells[0].find('{'+W+'}tcPr/{'+W+'}gridSpan').set('{'+W+'}val','1');ET.SubElement(cells[1].find('{'+W+'}tcPr'),'{'+W+'}gridSpan',{'{'+W+'}val':'2'})
            elif malformed=='width':doc.find('.//{'+W+'}gridCol').set('{'+W+'}w','0')
            elif malformed=='tracked':ET.SubElement(rows[0].find('{'+W+'}tc/{'+W+'}tcPr'),'{'+W+'}cellIns',{'{'+W+'}id':'1','{'+W+'}author':'Fixture'})
            else:ET.SubElement(rows[0].find('{'+W+'}tc/{'+W+'}tcPr'),'{'+W+'}hMerge',{'{'+W+'}val':'restart'})
            parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx');source=(tmp_path/'report.docx').read_bytes()
        with pytest.raises(HarnessError):plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev)],idempotency_key='bad')
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==source
    finally:store.close()


def test_unmerge_is_registered_and_requires_its_own_batch(tmp_path):
    store,plugin,original,rev=merged(tmp_path)
    try:
        assert LoadTools(operations=['unmerge_cells']).operations==['unmerge_cells']
        with pytest.raises(ValidationError,match='own bounded batch'):
            StageBatch(path='report.docx',revision=rev,operations=[op(plugin,rev),{'op':'set_cell_text','target':'cell:2','expected_hash':'0'*64,'text':'changed'}])
    finally:store.close()
