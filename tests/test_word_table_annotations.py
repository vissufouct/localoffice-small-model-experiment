"""Exact table/cell annotation boundaries through the real package worker."""
import pytest
from localoffice.annotations import annotate, bind_annotations
from localoffice.chats import current_chat
from localoffice.errors import HarnessError
from test_word_tables import saved_table, get


def bind(store, plugin, revision, ids, whole=False):
    annotations=[]
    for object_id in ids:
        obj=get(plugin,revision,object_id)
        annotations.append(annotate(store,path='report.docx',revision=revision,
            scope='artifact' if whole else 'selection',
            locator={'object_id':object_id,'object_hash':obj['hash']},comment='Edit only this selection'))
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('table-task','Edit selection','running','background','report.docx',1,1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)',('table-task',current_chat(store)))
        bind_annotations(store,'table-task','report.docx',annotations)


def stage(plugin, revision, object_id, op, **props):
    obj=get(plugin,revision,object_id)
    return plugin.stage_batch(path='report.docx',revision=revision,task_id='table-task',
        operations=[{'op':op,'target':object_id,'expected_hash':obj['hash'],**props}],
        idempotency_key='edit-'+revision+'-'+object_id)


@pytest.mark.parametrize('kind',['cell','table'])
def test_exact_table_cell_scope_preserves_other_content_and_undo(tmp_path,kind):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target='cell:4' if kind=='cell' else 'table:1'
        bind(store,plugin,rev,[target])
        cells=plugin.list_objects(path='report.docx',revision=rev,kind='cell')['objects']
        if kind=='cell':
            result=stage(plugin,rev,target,'set_cell_text',text='EUR 250\nApproved')
        else:
            result=stage(plugin,rev,target,'format_table',properties={'alignment':'center'})
        head=result['candidate_revision']
        after=plugin.list_objects(path='report.docx',revision=head,kind='cell')['objects']
        assert after[:3]==cells[:3]
        if kind=='cell':assert get(plugin,head,target)['text']=='EUR 250\nApproved'
        else:assert after==cells
        assert store.db.execute("SELECT head_revision FROM task_scopes WHERE task_id='table-task'").fetchone()[0]==head
        with pytest.raises(HarnessError,match='outside'):
            stage(plugin,head,'cell:1','set_cell_text',text='Out of scope')
        assert store.candidate('report.docx')['candidate_hash']==head
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=head,action='apply')
        undo=store.undo(result['receipt_id'],'undo-table-annotation')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_cell_scope_cannot_change_table_format_or_insert_rows(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        bind(store,plugin,rev,['cell:4'])
        with pytest.raises(HarnessError,match='outside'):
            stage(plugin,rev,'table:1','format_table',properties={'alignment':'center'})
        with pytest.raises(HarnessError,match='whole-artifact'):
            stage(plugin,rev,'row:2','insert_rows',rows=[['X','Y']])
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('whole',[False,True])
def test_cell_replacement_cannot_retarget_mixed_paragraph_annotations(tmp_path,whole):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        bind(store,plugin,rev,['cell:1','paragraph:1'],whole=whole)
        if whole:
            result=stage(plugin,rev,'cell:1','set_cell_text',text='One\nTwo')
            assert get(plugin,result['candidate_revision'],'cell:1')['text']=='One\nTwo'
        else:
            with pytest.raises(HarnessError,match='separate cell-scoped task'):
                stage(plugin,rev,'cell:1','set_cell_text',text='One\nTwo')
            assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_table_annotation_stale_hash_and_revision_rejected(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        with pytest.raises(HarnessError,match='changed'):
            annotate(store,path='report.docx',revision=rev,scope='selection',
                locator={'object_id':'cell:4','object_hash':'0'*64},comment='Old cell')
        table_hash=get(plugin,rev,'table:1')['hash']
        bind(store,plugin,rev,['cell:4'])
        stage(plugin,rev,'cell:4','set_cell_text',text='EUR 250')
        with pytest.raises(HarnessError,match='Refresh'):
            annotate(store,path='report.docx',revision=rev,scope='selection',
                locator={'object_id':'table:1','object_hash':table_hash},comment='Old table')
    finally:store.close()
