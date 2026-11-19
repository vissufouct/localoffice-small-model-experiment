import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import StageBatch,LoadTools
from localoffice.errors import HarnessError
from test_word_creation import design,W
from test_word_table_annotations import bind


def fixture(root,header=False):
    store=ProjectStore(root);plugin=WordPlugin(store);spec=design()
    spec['blocks']=[{'kind':'table','header_row':header,'rows':[[f'{r}{c}' for c in 'ABC'] for r in '123']}]
    made=plugin.create(**spec,idempotency_key='fixture');store.apply(made['receipt_id'],expected_candidate=made['candidate_revision'],action='save')
    return store,plugin,(root/'report.docx').read_bytes(),made['candidate_revision']


def op(plugin,rev,**rectangle):
    table=plugin.read_objects(path='report.docx',revision=rev,ids=['table:1'])['objects'][0]
    return {'op':'merge_cells','target':'table:1','expected_hash':table['hash'],'row_start':0,'row_count':2,'column_start':0,'column_count':2,**rectangle}


def xml(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:return ET.fromstring(z.read('word/document.xml'))


@pytest.mark.parametrize('height,width',[(1,2),(2,1),(2,2)])
def test_rectangular_merge_preserves_paragraphs_grid_other_cells_and_undo(tmp_path,height,width):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        read=plugin.read_objects(path='report.docx',revision=rev,ids=['table:1','row:2','cell:5'],include_format=True)['objects']
        assert read[0]['direct_format']['structure']=={'row_count':3,'grid_column_count':3}
        assert read[1]['direct_format']['structure']['row_index']==1
        assert read[2]['direct_format']['structure']['row_index']==1 and read[2]['direct_format']['structure']['column_start']==1
        operation=op(plugin,rev,row_count=height,column_count=width)
        args=dict(path='report.docx',revision=rev,operations=[operation],idempotency_key='merge')
        result=plugin.stage_batch(**args);assert plugin.stage_batch(**args)==result
        head=result['candidate_revision'];a=xml(original);b=xml(store.blob(head));before=a.findall('.//{'+W+'}tr');after=b.findall('.//{'+W+'}tr')
        selected=[cell for row in before[:height] for cell in row.findall('{'+W+'}tc')[:width]]
        expected=[ET.tostring(p) for cell in selected for p in cell.findall('{'+W+'}p')]
        anchor=after[0].find('{'+W+'}tc')
        assert [ET.tostring(p) for p in anchor.findall('{'+W+'}p')]==expected
        assert anchor.find('{'+W+'}tcPr/{'+W+'}gridSpan').get('{'+W+'}val')==str(width)
        if height>1:
            assert anchor.find('{'+W+'}tcPr/{'+W+'}vMerge').get('{'+W+'}val')=='restart'
            continuation=after[1].find('{'+W+'}tc');assert continuation.find('{'+W+'}tcPr/{'+W+'}vMerge').get('{'+W+'}val')=='continue'
            assert ''.join(continuation.itertext())==''
        for i,row in enumerate(before):
            for j,cell in enumerate(row.findall('{'+W+'}tc')):
                if i<height and j<width:continue
                candidates=[c for c in b.findall('.//{'+W+'}tc') if ''.join(c.itertext())==''.join(cell.itertext())]
                assert len(candidates)==1 and ET.tostring(candidates[0])==ET.tostring(cell)
        assert ET.tostring(a.find('.//{'+W+'}tblGrid'))==ET.tostring(b.find('.//{'+W+'}tblGrid'))
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(head))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=head,action='apply');undo=store.undo(result['receipt_id'],'undo-merge')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply');assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_merge_rejects_scoped_structural_change_and_existing_merge(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        bind(store,plugin,rev,['table:1']);operation=op(plugin,rev)
        with pytest.raises(HarnessError,match='whole-artifact'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[operation],idempotency_key='scope',task_id='table-task')
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[operation],idempotency_key='merge');head=result['candidate_revision']
        with pytest.raises(HarnessError,match='existing merge'):
            plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head)],idempotency_key='overlap')
        assert store.candidate('report.docx')['candidate_hash']==head
    finally:store.close()


def test_header_boundary_and_out_of_grid_fail_without_candidate(tmp_path):
    store,plugin,original,rev=fixture(tmp_path,header=True)
    try:
        for changes,message in [({},'header boundary'),({'row_start':3},'exceeds'),({'column_start':3},'exceeds')]:
            with pytest.raises(HarnessError,match=message):
                plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev,**changes)],idempotency_key=message+str(changes))
        assert store.candidate('report.docx') is None
    finally:store.close()


def test_merge_contract_bounds_and_standalone_batch(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        for changes in [{'row_count':1,'column_count':1},{'row_start':-1},{'row_count':21},{'column_count':13}]:
            with pytest.raises(ValidationError):StageBatch(path='report.docx',revision=rev,operations=[op(plugin,rev,**changes)])
        with pytest.raises(ValidationError,match='own bounded batch'):
            StageBatch(path='report.docx',revision=rev,operations=[op(plugin,rev),{'op':'set_cell_text','target':'cell:9','expected_hash':'0'*64,'text':'X'}])
        assert LoadTools(operations=['merge_cells']).operations==['merge_cells']
    finally:store.close()


@pytest.mark.parametrize('kind',['bookmark','field','nested'])
def test_unsupported_selected_content_fails_without_mutation(tmp_path,kind):
    from test_word_lists import rewrite
    store,plugin,original,rev=fixture(tmp_path)
    try:
        def change(parts):
            doc=ET.fromstring(parts['word/document.xml']);cell=doc.findall('.//{'+W+'}tc')[1];p=cell.find('{'+W+'}p')
            if kind=='bookmark':
                p.insert(0,ET.Element('{'+W+'}bookmarkStart',{'{'+W+'}id':'1','{'+W+'}name':'Target'}));ET.SubElement(p,'{'+W+'}bookmarkEnd',{'{'+W+'}id':'1'})
            elif kind=='field':
                run=ET.SubElement(p,'{'+W+'}r');ET.SubElement(run,'{'+W+'}fldChar',{'{'+W+'}fldCharType':'begin'})
            else:
                table=ET.SubElement(cell,'{'+W+'}tbl');grid=ET.SubElement(table,'{'+W+'}tblGrid');ET.SubElement(grid,'{'+W+'}gridCol',{'{'+W+'}w':'500'});row=ET.SubElement(table,'{'+W+'}tr');nested=ET.SubElement(row,'{'+W+'}tc');ET.SubElement(nested,'{'+W+'}p');ET.SubElement(cell,'{'+W+'}p')
            parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,change);rev=store.file_hash('report.docx');source=(tmp_path/'report.docx').read_bytes()
        with pytest.raises(HarnessError,match='requiring explicit support'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev)],idempotency_key='unsupported')
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==source
    finally:store.close()


def test_disjoint_existing_merge_keeps_grid_coordinates_and_untouched_merged_cell(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        first=plugin.stage_batch(path='report.docx',revision=rev,operations=[op(plugin,rev,row_count=1)],idempotency_key='horizontal')
        head=first['candidate_revision'];before=xml(store.blob(head)).find('.//{'+W+'}tc')
        cells=plugin.list_objects(path='report.docx',revision=head,kind='cell')['objects']
        last=next(c for c in cells if c['preview']=='1C')
        info=plugin.read_objects(path='report.docx',revision=head,ids=[last['id']],include_format=True)['objects'][0]['direct_format']['structure']
        assert info['column_start']==2 and info['row_index']==0
        second=plugin.stage_batch(path='report.docx',revision=head,operations=[op(plugin,head,column_start=2,column_count=1)],idempotency_key='vertical')
        after=xml(store.blob(second['candidate_revision'])).find('.//{'+W+'}tc')
        assert ET.tostring(after)==ET.tostring(before)
    finally:store.close()
