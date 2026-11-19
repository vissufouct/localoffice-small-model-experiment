import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import StageBatch
from test_word_tables import saved_table, get
from test_word_creation import W
from test_word_table_annotations import bind


def operation(plugin,rev,position='after'):
    p=plugin.list_objects(path='report.docx',revision=rev,kind='paragraph')['objects'][0]
    return {'op':'insert_table','target':p['id'],'expected_hash':p['hash'],'position':position,
        'rows':[['Category','Amount'],['Training','EUR 250']],
        'column_widths_twips':[3600,2400],'header_row':True,'style':'Normal'}


@pytest.mark.parametrize('position',['before','after'])
def test_insert_table_preserves_package_replay_and_exact_undo(tmp_path,position):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        op=operation(plugin,rev,position)
        args=dict(path='report.docx',revision=rev,operations=[op],idempotency_key='table')
        result=plugin.stage_batch(**args)
        assert plugin.stage_batch(**args)==result
        head=result['candidate_revision']
        tables=plugin.list_objects(path='report.docx',revision=head,kind='table')['objects']
        assert len(tables)==2 and tables[0]['preview'].startswith('Category')
        paragraphs=plugin.list_objects(path='report.docx',revision=head,kind='paragraph')['objects']
        assert any(p['hash']==op['expected_hash'] for p in paragraphs)
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(head))) as new:
            assert old.namelist()==new.namelist()
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            assert ET.tostring(a.find('.//{'+W+'}tbl'))==ET.tostring(b.findall('.//{'+W+'}tbl')[1])
            first=b.find('.//{'+W+'}tbl')
            assert [ET.tostring(e) for e in a.find('{'+W+'}body')]==[ET.tostring(e) for e in b.find('{'+W+'}body') if e is not first]
            assert [e.get('{'+W+'}w') for e in first.findall('./{'+W+'}tblGrid/{'+W+'}gridCol')]==['3600','2400']
            assert len(first.findall('.//{'+W+'}tblHeader'))==1
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=head,action='apply')
        undo=store.undo(result['receipt_id'],'undo-new-table')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('position',['before','after'])
def test_table_insertion_separates_adjacent_tables_and_rejects_stale_scope(tmp_path,position):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        # Insert beside an existing table from either neighbouring paragraph.
        with zipfile.ZipFile(io.BytesIO(original)) as package:
            body=ET.fromstring(package.read('word/document.xml')).find('{'+W+'}body')
            preceding=list(body)[list(body).index(body.find('{'+W+'}tbl'))+(-1 if position=='after' else 1)]
            text=''.join(preceding.itertext())
        paragraphs=plugin.list_objects(path='report.docx',revision=rev,kind='paragraph')['objects']
        anchor=next(p for p in paragraphs if get(plugin,rev,p['id'])['text']==text)
        op={**operation(plugin,rev,position),'target':anchor['id'],'expected_hash':anchor['hash']}
        bind(store,plugin,rev,[anchor['id']])
        with pytest.raises(HarnessError,match='whole-artifact'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='blocked',task_id='table-task')
        assert store.candidate('report.docx') is None
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='insert')
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as package:
            body=ET.fromstring(package.read('word/document.xml')).find('{'+W+'}body')
            children=list(body);indices=[i for i,e in enumerate(children) if e.tag=='{'+W+'}tbl']
            assert len(indices)==2 and indices[1]-indices[0]==2
            assert children[indices[0]+1].tag=='{'+W+'}p'
        with pytest.raises(HarnessError):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[op],idempotency_key='stale')
    finally:store.close()


def test_new_table_contract_bounds_and_standalone_batch(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        op=operation(plugin,rev)
        for changes in ({'rows':[['one']]},{'column_widths_twips':[100,2400]}, {'rows':[['\x00','bad']]}):
            with pytest.raises(ValidationError):
                StageBatch(path='report.docx',revision=rev,operations=[{**op,**changes}])
        with pytest.raises(ValidationError,match='own bounded batch'):
            StageBatch(path='report.docx',revision=rev,operations=[op,{**op,'target':'paragraph:2'}])
        wrong={**op,'style':'MissingStyle'}
        with pytest.raises(HarnessError,match='Unknown paragraph style'):
            plugin.stage_batch(path='report.docx',revision=rev,operations=[wrong],idempotency_key='bad-style')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_insert_table_guidance_is_lazy_and_registered():
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema,GUIDES
    from localoffice.word.contracts import LoadTools
    assert LoadTools(operations=['insert_table']).operations==['insert_table']
    initial=project_schema(WORD_ACTION.json_schema(),[])
    assert 'InsertTable' not in initial['$defs']
    selected=project_schema(WORD_ACTION.json_schema(),['insert_table'])
    assert selected['$defs']['StageBatch']['properties']['operations']['items']['discriminator']['mapping']=={'insert_table':'#/$defs/InsertTable'}
    assert 'whole-artifact' in GUIDES['insert_table'].lower()
