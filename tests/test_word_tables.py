import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import StageBatch
from localoffice.errors import HarnessError
from test_word_creation import design,W


def saved_table(root):
    store=ProjectStore(root);plugin=WordPlugin(store)
    made=plugin.create(**design(),idempotency_key='create')
    store.apply(made['receipt_id'],expected_candidate=made['candidate_revision'],action='save')
    return store,plugin,(root/'report.docx').read_bytes(),made['candidate_revision']


def get(plugin,revision,object_id):
    return plugin.read_objects(path='report.docx',revision=revision,ids=[object_id])['objects'][0]


def test_cell_value_edit_preserves_other_cells_and_layout(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        cells=plugin.list_objects(path='report.docx',revision=revision,parent='table:1',kind='cell')['objects']
        assert len(cells)==4
        cell=cells[3]
        op={'id':'budget','op':'set_cell_text','target':cell['id'],'expected_hash':cell['hash'],'text':'EUR 250\nApproved'}
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=[op],idempotency_key='cell')
        current=result['candidate_revision']
        assert get(plugin,current,'cell:4')['text']=='EUR 250\nApproved'
        after=plugin.list_objects(path='report.docx',revision=current,kind='cell')['objects']
        assert after[:3]==cells[:3]
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(current))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            for tag in ['tblPr','tblGrid','tcPr']:
                assert [ET.tostring(e) for e in a.iter('{'+W+'}'+tag)]==[ET.tostring(e) for e in b.iter('{'+W+'}'+tag)]
        same=plugin.stage_batch(path='report.docx',revision=current,operations=[{**op,'expected_hash':get(plugin,current,'cell:4')['hash']}],idempotency_key='same')
        assert same['status']=='unchanged'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=current,action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('position',['before','after'])
def test_row_insertion_preserves_anchor_widths_and_cell_styles(tmp_path,position):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        anchor=get(plugin,revision,'row:2')
        op={'id':'rows','op':'insert_rows','target':anchor['id'],'expected_hash':anchor['hash'],
            'position':position,'rows':[['Acacia','EUR 240'],['Maple','EUR 350']]}
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=[op],idempotency_key='rows')
        current=result['candidate_revision']
        assert plugin.stage_batch(path='report.docx',revision=revision,operations=[op],idempotency_key='rows')==result
        rows=plugin.list_objects(path='report.docx',revision=current,kind='row',parent='table:1')['objects']
        expected=['Item\tBudget','Acacia\tEUR 240','Maple\tEUR 350','Cedar\tEUR 120'] if position=='before' else ['Item\tBudget','Cedar\tEUR 120','Acacia\tEUR 240','Maple\tEUR 350']
        assert [r['preview'] for r in rows]==expected
        assert rows[3 if position=='before' else 1]['hash']==anchor['hash']
        with zipfile.ZipFile(io.BytesIO(store.blob(current))) as package:
            table=ET.fromstring(package.read('word/document.xml')).find('.//{'+W+'}tbl')
            assert len(table.findall('.//{'+W+'}tblHeader'))==1
            widths=[p.get('{'+W+'}w') for p in table.findall('.//{'+W+'}tcW')]
            assert len(set(widths))==1 and len(widths)==8
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=current,action='apply')
        undo=store.undo(result['receipt_id'],'undo-rows');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_table_edits_reject_overlap_bad_width_and_vertical_merges(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        cell=get(plugin,revision,'cell:1');paragraph=get(plugin,revision,'paragraph:5');row=get(plugin,revision,'row:2')
        with pytest.raises(HarnessError,match='overlap'):
            plugin.stage_batch(path='report.docx',revision=revision,operations=[
                {'id':'cell','op':'set_cell_text','target':cell['id'],'expected_hash':cell['hash'],'text':'Changed'},
                {'id':'p','op':'format_paragraph','target':paragraph['id'],'expected_hash':paragraph['hash'],'properties':{'bold':True}}],idempotency_key='overlap')
        with pytest.raises(HarnessError,match='physical anchor cell'):
            plugin.stage_batch(path='report.docx',revision=revision,operations=[{'id':'row','op':'insert_rows','target':row['id'],'expected_hash':row['hash'],'rows':[['Too few']]}],idempotency_key='width')
        assert not store.candidate('report.docx')
        with zipfile.ZipFile(io.BytesIO(original)) as package:parts={n:package.read(n) for n in package.namelist()}
        doc=ET.fromstring(parts['word/document.xml']);props=doc.find('.//{'+W+'}tcPr');ET.SubElement(props,'{'+W+'}vMerge',{'{'+W+'}val':'restart'})
        parts['word/document.xml']=ET.tostring(doc)
        with zipfile.ZipFile(tmp_path/'report.docx','w') as package:
            for name,data in parts.items():package.writestr(name,data)
        revision=store.file_hash('report.docx');row=get(plugin,revision,'row:2')
        with pytest.raises(HarnessError,match='vertical merges'):
            plugin.stage_batch(path='report.docx',revision=revision,operations=[{'id':'row','op':'insert_rows','target':row['id'],'expected_hash':row['hash'],'rows':[['New','Value']]}],idempotency_key='merged')
        assert not store.candidate('report.docx')
    finally:store.close()


def test_table_payload_bounds():
    base={'id':'r','op':'insert_rows','target':'row:1','expected_hash':'a'*64}
    for rows in [[['x']]*9,[['x']*13],[['a'],['b','c']],[['\x01']]]:
        with pytest.raises(ValidationError):StageBatch(path='a.docx',revision='a'*64,operations=[{**base,'rows':rows}])


def test_unsupported_second_cell_leaves_no_partial_candidate(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        with zipfile.ZipFile(io.BytesIO(original)) as package:parts={n:package.read(n) for n in package.namelist()}
        doc=ET.fromstring(parts['word/document.xml']);paragraph=doc.findall('.//{'+W+'}tc')[1].find('{'+W+'}p')
        field=ET.SubElement(paragraph,'{'+W+'}fldSimple',{'{'+W+'}instr':'DATE'})
        ET.SubElement(ET.SubElement(field,'{'+W+'}r'),'{'+W+'}t').text='date'
        parts['word/document.xml']=ET.tostring(doc)
        with zipfile.ZipFile(tmp_path/'report.docx','w') as package:
            for name,data in parts.items():package.writestr(name,data)
        original=(tmp_path/'report.docx').read_bytes();revision=store.file_hash('report.docx')
        cells=plugin.list_objects(path='report.docx',revision=revision,kind='cell')['objects']
        with pytest.raises(HarnessError,match='fields'):
            plugin.stage_batch(path='report.docx',revision=revision,operations=[{'id':str(i),'op':'set_cell_text','target':c['id'],'expected_hash':c['hash'],'text':'Replacement'} for i,c in enumerate(cells[:2])],idempotency_key='atomic-cells')
        assert not store.candidate('report.docx') and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()
