import io
import json
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import StageBatch,BatchReceipt
from localoffice.errors import HarnessError
from localoffice.annotations import annotate,bind_annotations
from test_word_worker import fixture_docx,W


@pytest.mark.parametrize('position',['before','after'])
def test_insert_preserves_anchors_parts_and_undo(tmp_path,position):
    source=tmp_path/'a.docx';fixture_docx(source);original=source.read_bytes()
    store=ProjectStore(tmp_path);plugin=WordPlugin(store);revision=store.file_hash('a.docx')
    try:
        indexed=plugin.list_objects(path='a.docx',revision=revision,limit=1)
        anchor=indexed['objects'][0]
        request={'path':'a.docx','revision':revision,'operations':[{'id':'add','op':'insert_paragraphs',
            'target':anchor['id'],'expected_hash':anchor['hash'],'position':position,
            'paragraphs':[{'text':'New heading 😀','style':'Heading1','page_break_before':True},
                          {'text':'Second inserted paragraph','style':'Heading1'}]}]}
        receipt=plugin.stage_batch(**request,idempotency_key='insert')
        BatchReceipt(**receipt)
        assert plugin.stage_batch(**request,idempotency_key='insert')==receipt
        assert source.read_bytes()==original
        after=plugin.list_objects(path='a.docx',revision=receipt['candidate_revision'],kind='paragraph')['objects']
        expected=['New heading 😀','Second inserted paragraph','A😀BCDEFG','Unchanged paragraph'] if position=='before' else ['A😀BCDEFG','New heading 😀','Second inserted paragraph','Unchanged paragraph']
        assert [x['preview'] for x in after]==expected
        start=0 if position=='before' else 1
        assert receipt['results'][0]['inserted_ids']==[after[start]['id'],after[start+1]['id']]
        assert after[2 if position=='before' else 0]['hash']==anchor['hash']
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(receipt['candidate_revision']))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            body=ET.fromstring(new.read('word/document.xml')).find('{'+W+'}body')
            assert body[-1].tag=='{'+W+'}sectPr'
            assert body[start].find('.//{'+W+'}pageBreakBefore') is not None
        with pytest.raises(HarnessError,match='stale_cursor'):
            plugin.list_objects(path='a.docx',revision=receipt['candidate_revision'],cursor=indexed['next_cursor'])
        store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply')
        undo=store.undo(receipt['receipt_id'],'undo-insert')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert source.read_bytes()==original
    finally:store.close()


def test_insert_failure_is_atomic_and_contract_is_bounded(tmp_path):
    source=tmp_path/'a.docx';fixture_docx(source);store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('a.docx');anchor=plugin.list_objects(path='a.docx',revision=revision)['objects'][0]
        op={'id':'add','op':'insert_paragraphs','target':anchor['id'],'expected_hash':anchor['hash'],
            'paragraphs':[{'text':'Valid','style':'Heading1'},{'text':'Invalid','style':'Absent'}]}
        with pytest.raises(HarnessError,match='Unknown paragraph style'):
            plugin.stage_batch(path='a.docx',revision=revision,operations=[op],idempotency_key='bad')
        assert not store.candidate('a.docx')
        for operations in [[op,{**op,'id':'two','target':'paragraph:2'}],[{**op,'paragraphs':[{'text':'x'}]*25}],
                           [{**op,'paragraphs':[{'text':'\x01'}]}],[{**op,'paragraphs':[{'text':'界'*8000}]*2}]]:
            with pytest.raises(ValidationError):StageBatch(path='a.docx',revision=revision,operations=operations)
    finally:store.close()


def test_grow_document_beyond_creation_limit_with_bounded_batches(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        created=plugin.create(path='long.docx',blocks=[{'kind':'paragraph','text':'Start'}],idempotency_key='new')
        revision=created['candidate_revision'];anchor_id='paragraph:1'
        for batch in range(3):
            anchor=plugin.read_objects(path='long.docx',revision=revision,ids=[anchor_id])['objects'][0]
            paragraphs=[{'text':f'Section {batch*24+i+1}'} for i in range(24)]
            args={'path':'long.docx','revision':revision,'operations':[{'id':f'append-{batch}','op':'insert_paragraphs','target':anchor_id,'expected_hash':anchor['hash'],'paragraphs':paragraphs}]}
            assert len(json.dumps(args).encode())<2000
            receipt=plugin.stage_batch(**args,idempotency_key=f'batch-{batch}')
            revision=receipt['candidate_revision'];anchor_id=receipt['results'][0]['inserted_ids'][-1]
        objects=[];cursor=None
        while True:
            page=plugin.list_objects(path='long.docx',revision=revision,kind='paragraph',limit=20,cursor=cursor)
            objects.extend(page['objects']);cursor=page['next_cursor']
            if not cursor:break
        assert [o['preview'] for o in objects]==['Start']+[f'Section {i}' for i in range(1,73)]
        assert not (tmp_path/'long.docx').exists()
    finally:store.close()


@pytest.mark.parametrize('anchor_id',['table:1','paragraph:5'])
def test_insertion_keeps_table_or_cell_parent(tmp_path,anchor_id):
    from test_word_creation import design
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        created=plugin.create(**design(),idempotency_key='new-table')
        revision=created['candidate_revision']
        anchor=plugin.read_objects(path='report.docx',revision=revision,ids=[anchor_id])['objects'][0]
        result=plugin.stage_batch(path='report.docx',revision=revision,operations=[{
            'id':'add','op':'insert_paragraphs','target':anchor_id,'expected_hash':anchor['hash'],
            'paragraphs':[{'text':'Inserted beside anchor'}]}],idempotency_key='table-insert')
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as package:
            body=ET.fromstring(package.read('word/document.xml')).find('{'+W+'}body')
            expected_parent=body if anchor_id=='table:1' else body.find('.//{'+W+'}tc')
            children=list(expected_parent)
            inserted=next(c for c in children if ''.join(c.itertext())=='Inserted beside anchor')
            previous=children[children.index(inserted)-1]
            assert previous.tag=='{'+W+'}'+('tbl' if anchor_id=='table:1' else 'p')
            if anchor_id=='paragraph:5':assert ''.join(previous.itertext())=='Item'
            assert body[-1].tag=='{'+W+'}sectPr'
    finally:store.close()


@pytest.mark.parametrize('scope',['selection','artifact'])
def test_insertion_requires_broader_annotation_scope(tmp_path,scope):
    from localoffice.chats import current_chat
    source=tmp_path/'a.docx';fixture_docx(source);store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('a.docx');anchor=plugin.list_objects(path='a.docx',revision=revision)['objects'][0]
        annotation=annotate(store,path='a.docx',revision=revision,scope=scope,
                           locator={'object_id':anchor['id'],'object_hash':anchor['hash']},comment='Add a paragraph')
        with store.transaction():
            store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('insert-task','Add a paragraph','running','background','a.docx',1,1))
            store.db.execute('INSERT INTO task_chats VALUES(?,?)',('insert-task',current_chat(store)))
            bind_annotations(store,'insert-task','a.docx',[annotation])
        args={'path':'a.docx','revision':revision,'operations':[{'id':'add','op':'insert_paragraphs','target':anchor['id'],'expected_hash':anchor['hash'],'paragraphs':[{'text':'Added','style':'Heading1'}]}], 'idempotency_key':'scoped','task_id':'insert-task'}
        if scope=='selection':
            with pytest.raises(HarnessError,match='whole-artifact'):plugin.stage_batch(**args)
            assert not store.candidate('a.docx')
        else:
            result=plugin.stage_batch(**args)
            assert store.db.execute("SELECT head_revision FROM task_scopes WHERE task_id='insert-task'").fetchone()[0]==result['candidate_revision']
    finally:store.close()
