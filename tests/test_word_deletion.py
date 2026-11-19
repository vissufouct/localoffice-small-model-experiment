import io
import zipfile
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import StageBatch, BatchReceipt, EditResult
from localoffice.errors import HarnessError
from test_word_worker import fixture_docx, W


def prepare(tmp_path, transform=None):
    path=tmp_path/'a.docx';fixture_docx(path)
    if transform:
        with zipfile.ZipFile(path) as archive:parts={n:archive.read(n) for n in archive.namelist()}
        parts['word/document.xml']=transform(parts['word/document.xml'].decode()).encode()
        with zipfile.ZipFile(path,'w') as archive:
            for name,data in parts.items():archive.writestr(name,data)
    store=ProjectStore(tmp_path);plugin=WordPlugin(store);revision=store.file_hash('a.docx')
    return store,plugin,revision


def request(plugin,revision,target='paragraph:1'):
    obj=plugin.read_objects(path='a.docx',revision=revision,ids=[target])['objects'][0]
    return dict(path='a.docx',revision=revision,operations=[dict(op='delete_paragraph',target=target,expected_hash=obj['hash'])])


def test_delete_preserves_other_parts_and_paragraph_with_exact_undo(tmp_path):
    store,plugin,revision=prepare(tmp_path);original=(tmp_path/'a.docx').read_bytes()
    try:
        other=plugin.read_objects(path='a.docx',revision=revision,ids=['paragraph:2'])['objects'][0]
        args=request(plugin,revision)
        receipt=plugin.stage_batch(**args,idempotency_key='delete');BatchReceipt(**receipt)
        assert receipt['results'][0]['deleted'] and receipt['results'][0]['after_hash'] is None
        assert plugin.stage_batch(**args,idempotency_key='delete')==receipt
        assert (tmp_path/'a.docx').read_bytes()==original
        remaining=plugin.list_objects(path='a.docx',revision=receipt['candidate_revision'],kind='paragraph')['objects']
        assert len(remaining)==1 and remaining[0]['hash']==other['hash']
        with zipfile.ZipFile(io.BytesIO(original)) as before,zipfile.ZipFile(io.BytesIO(store.blob(receipt['candidate_revision']))) as after:
            assert before.namelist()==after.namelist()
            for name in before.namelist():
                if name!='word/document.xml':assert before.read(name)==after.read(name)
        with pytest.raises(HarnessError) as stale:plugin.stage_batch(**args,idempotency_key='stale')
        assert stale.value.code=='stale_revision'
        store.apply(receipt['receipt_id'],expected_candidate=receipt['candidate_revision'],action='apply')
        undo=store.undo(receipt['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'a.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('fragment',[
    '<w:bookmarkStart w:id="1" w:name="mark"/><w:bookmarkEnd w:id="1"/>',
    '<w:fldSimple w:instr="DATE"><w:r><w:t>date</w:t></w:r></w:fldSimple>',
    '<w:pPr><w:sectPr/></w:pPr>',
    '<w:pPr><w:rPr><w:del w:id="1" w:author="Reviewer"/></w:rPr></w:pPr>',
])
def test_complex_paragraph_rejected_before_staging(tmp_path,fragment):
    store,plugin,revision=prepare(tmp_path,lambda xml:xml.replace('<w:p>','<w:p>'+fragment,1))
    try:
        with pytest.raises(HarnessError,match='additional support'):plugin.stage_batch(**request(plugin,revision),idempotency_key='bad')
        assert store.candidate('a.docx') is None
    finally:store.close()


def test_last_paragraph_and_spanning_field_are_not_deleted(tmp_path):
    store,plugin,revision=prepare(tmp_path)
    try:
        first=plugin.stage_batch(**request(plugin,revision),idempotency_key='first')
        with pytest.raises(HarnessError,match='last required'):
            plugin.stage_batch(**request(plugin,first['candidate_revision']),idempotency_key='last')
        assert store.candidate('a.docx')['candidate_hash']==first['candidate_revision']
    finally:store.close()
    folder=tmp_path/'spanning';folder.mkdir()
    store,plugin,revision=prepare(folder,lambda xml:xml.replace('<w:p>','<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>',1))
    try:
        with pytest.raises(HarnessError,match='spanning field'):
            plugin.stage_batch(**request(plugin,revision,'paragraph:2'),idempotency_key='field')
        assert store.candidate('a.docx') is None
    finally:store.close()


def test_deletion_contract_and_lazy_schema(tmp_path):
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    store,plugin,revision=prepare(tmp_path)
    try:
        args=request(plugin,revision);second={**args['operations'][0],'target':'paragraph:2'}
        with pytest.raises(ValidationError):StageBatch(**{**args,'operations':[*args['operations'],second]})
        assert 'DeleteParagraph' not in project_schema(WORD_ACTION.json_schema(),[])['$defs']
        assert 'DeleteParagraph' in project_schema(WORD_ACTION.json_schema(),['delete_paragraph'])['$defs']
        base=dict(id='delete',target='paragraph:1',before_hash='a'*64,summary='Removed')
        for values in ({'after_hash':None},{'after_hash':'b'*64,'deleted':True}):
            with pytest.raises(ValidationError):EditResult(**base,**values)
    finally:store.close()


@pytest.mark.parametrize('scope',['selection','artifact'])
def test_delete_requires_whole_artifact_annotation_scope(tmp_path,scope):
    from localoffice.annotations import annotate,bind_annotations
    from localoffice.chats import current_chat
    store,plugin,revision=prepare(tmp_path)
    try:
        args=request(plugin,revision);op=args['operations'][0]
        annotation=annotate(store,path='a.docx',revision=revision,scope=scope,
            locator={'object_id':op['target'],'object_hash':op['expected_hash']},comment='Remove this paragraph')
        with store.transaction():
            store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('delete-task','Remove paragraph','running','background','a.docx',1,1))
            store.db.execute('INSERT INTO task_chats VALUES(?,?)',('delete-task',current_chat(store)))
            bind_annotations(store,'delete-task','a.docx',[annotation])
        if scope=='selection':
            with pytest.raises(HarnessError,match='whole-artifact'):
                plugin.stage_batch(**args,task_id='delete-task',idempotency_key='scoped')
            assert store.candidate('a.docx') is None
        else:
            receipt=plugin.stage_batch(**args,task_id='delete-task',idempotency_key='scoped')
            assert store.db.execute("SELECT head_revision FROM task_scopes WHERE task_id='delete-task'").fetchone()[0]==receipt['candidate_revision']
    finally:store.close()


def test_cell_paragraph_deletion_preserves_required_cell_content(tmp_path):
    from test_word_tables import saved_table
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        obj=plugin.read_objects(path='report.docx',revision=revision,ids=['paragraph:5'])['objects'][0]
        inserted=plugin.stage_batch(path='report.docx',revision=revision,operations=[dict(op='insert_paragraphs',target=obj['id'],expected_hash=obj['hash'],paragraphs=[{'text':'Temporary cell paragraph'}])],idempotency_key='insert')
        target=inserted['results'][0]['inserted_ids'][0]
        obj=plugin.read_objects(path='report.docx',revision=inserted['candidate_revision'],ids=[target])['objects'][0]
        receipt=plugin.stage_batch(path='report.docx',revision=inserted['candidate_revision'],operations=[dict(op='delete_paragraph',target=target,expected_hash=obj['hash'])],idempotency_key='remove')
        cell=plugin.read_objects(path='report.docx',revision=receipt['candidate_revision'],ids=['cell:1'])['objects'][0]
        assert cell['text']=='Item'
    finally:store.close()


def test_deletion_does_not_join_neighboring_tables(tmp_path):
    table='<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    store,plugin,revision=prepare(tmp_path,lambda xml:xml.replace('<w:body>','<w:body>'+table).replace('<w:p><w:r><w:t>Unchanged paragraph',table+'<w:p><w:r><w:t>Unchanged paragraph'))
    try:
        with pytest.raises(HarnessError,match='join adjacent tables'):
            plugin.stage_batch(**request(plugin,revision,'paragraph:2'),idempotency_key='separator')
        assert store.candidate('a.docx') is None
    finally:store.close()


def test_deleted_object_receipt_reconciles_after_restart_without_replay(tmp_path,monkeypatch):
    import json
    from localoffice.native_history import record_call,operation_key,reconcile_word_call,exchanges
    from test_word_progress import task
    store,plugin,revision=prepare(tmp_path);task(store)
    with store.transaction():store.db.execute("UPDATE tasks SET artifact='a.docx' WHERE id='task'")
    args=request(plugin,revision);call=record_call(store,'task',{'tool':'word.stage_batch',**args})
    receipt=plugin.stage_batch(**args,task_id='task',idempotency_key=operation_key(call))
    store.close();store=ProjectStore(tmp_path)
    try:
        def forbidden(*args,**kwargs):raise AssertionError('Recovery must not execute deletion again')
        monkeypatch.setattr(WordPlugin,'_execute',forbidden)
        assert reconcile_word_call(store,'task')['state']=='reconciled'
        pair=exchanges(store,'task')[0]
        assert pair[1]['tool_call_id']==call
        assert 'deleted' in pair[1]['content']
        assert reconcile_word_call(store,'task')['state']=='no_pending_call'
        assert store.candidate('a.docx')['candidate_hash']==receipt['candidate_revision']
        assert store.db.execute("SELECT count(*) FROM operations WHERE task_id='task'").fetchone()[0]==1
    finally:store.close()


def test_clearing_text_retains_paragraph_and_receipt_says_so(tmp_path):
    from localoffice.native_tools import action_tools,compact_tools
    from localoffice.runtime import WORD_ACTION
    from localoffice.word.guidance import project_schema
    store,plugin,revision=prepare(tmp_path)
    try:
        obj=plugin.read_objects(path='a.docx',revision=revision,ids=['paragraph:2'])['objects'][0]
        result=plugin.stage_batch(path='a.docx',revision=revision,operations=[{
            'op':'replace_text','target':obj['id'],'expected_hash':obj['hash'],
            'start':0,'end':len(obj['text']),'text':''}],idempotency_key='clear')
        objects=plugin.read_objects(path='a.docx',revision=result['candidate_revision'],ids=['paragraph:1','paragraph:2'])['objects']
        assert objects[1]['text']=='' and len(objects)==2
        assert result['results'][0]['after_hash'] is not None
        assert 'paragraph remains present' in result['results'][0]['summary']
        tools=compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['replace_text'])))
        tool=next(t['function'] for t in tools if t['function']['name']=='word.replace_text')
        assert 'retains the paragraph' in tool['description']
        assert 'delete_paragraph' in tool['description']
    finally:store.close()
