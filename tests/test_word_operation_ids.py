import copy
import pytest
from pydantic import ValidationError
from localoffice.word.contracts import StageBatch,FormatParagraph
from localoffice.word.guidance import project_schema
from localoffice.runtime import WORD_ACTION
from test_word_tables import saved_table


def operation(target='paragraph:1',**extra):
    return {'op':'format_paragraph','target':target,'expected_hash':'a'*64,'properties':{'bold':True},**extra}


def test_omitted_and_null_ids_are_assigned_without_changing_inputs_or_explicit_ids():
    ops=[operation(),operation('paragraph:2',id='operation-1'),operation('paragraph:3',id=None)]
    before=copy.deepcopy(ops)
    batch=StageBatch(path='a.docx',revision='b'*64,operations=ops)
    assert [op.id for op in batch.operations]==['operation-2','operation-1','operation-3']
    assert ops==before
    assert StageBatch(**batch.model_dump()).model_dump()==batch.model_dump()
    direct=FormatParagraph(**operation())
    assert StageBatch(path='a.docx',revision='b'*64,operations=[direct]).operations[0].id=='operation-1'
    assert direct.id is None


@pytest.mark.parametrize('mutate',[lambda o:o.pop('target'),lambda o:o.pop('expected_hash'),lambda o:o.update(expected_hash='bad'),lambda o:o.update(id='')])
def test_auto_ids_do_not_fill_semantic_fields_or_accept_invalid_explicit_ids(mutate):
    op=operation();mutate(op)
    with pytest.raises(ValidationError):StageBatch(path='a.docx',revision='b'*64,operations=[op])


def test_duplicate_explicit_ids_and_targets_remain_rejected_and_schema_marks_id_optional():
    with pytest.raises(ValidationError,match='IDs must be unique'):
        StageBatch(path='a.docx',revision='b'*64,operations=[operation(id='same'),operation('paragraph:2',id='same')])
    with pytest.raises(ValidationError,match='one target'):
        StageBatch(path='a.docx',revision='b'*64,operations=[operation(),operation()])
    schema=project_schema(WORD_ACTION.json_schema(),['format_paragraph'])['$defs']['FormatParagraph']
    assert 'id' not in schema['required']
    assert {'target','expected_hash','properties'}<=set(schema['required'])


def test_host_generated_ids_reach_worker_ledger_and_idempotent_replay(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        op=operation(target['id']);op['expected_hash']=target['hash']
        args={'path':'report.docx','revision':rev,'operations':[op],'idempotency_key':'auto-id'}
        result=plugin.stage_batch(**args)
        assert result['results'][0]['id']=='operation-1'
        assert plugin.stage_batch(**args)==result
        args['operations']=[{**op,'id':'operation-1'}]
        assert plugin.stage_batch(**args)==result
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-auto');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()
