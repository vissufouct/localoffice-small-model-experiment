"""Contract boundary checks; worker behavior and token benchmarks are separate gates."""
import pytest
from pydantic import ValidationError
from localoffice.word.contracts import ListObjects, ReadObjects, StageBatch

HASH = 'a' * 64

def patch(**changes):
    return dict(op='replace_text', id='edit-1', target='p:1', expected_hash=HASH,
                start=4, end=8, text='new', **changes)

def batch(operations):
    return StageBatch(path='reports/brief.docx', revision=HASH, operations=operations)


def test_compact_patch_contains_no_unchanged_document():
    result = batch([patch()])
    assert len(result.model_dump_json().encode()) < 400
    assert result.operations[0].text == 'new'
    assert not any(k in type(result).model_fields for k in ('document', 'script', 'xml', 'apply'))


@pytest.mark.parametrize('path', ['../brief.docx', '/tmp/brief.docx', '.localoffice/a.docx',
                                  'folder/../a.docx', 'a.md', 'a\\b.docx', 'a//b.docx'])
def test_paths_cannot_escape_or_target_non_word(path):
    with pytest.raises(ValidationError):
        ListObjects(path=path, revision=HASH)


def test_batch_rejects_unbounded_or_ambiguous_work():
    with pytest.raises(ValidationError): batch([patch(), patch()])
    other=patch(); other['id']='edit-2'
    with pytest.raises(ValidationError): batch([patch(), other])
    operations=[{**patch(), 'id':f'e{i}', 'target':f'p:{i}'} for i in range(25)]
    with pytest.raises(ValidationError): batch(operations)
    # Unicode counts as UTF-8 bytes, not just Python characters.
    large=[{**patch(), 'id':f'e{i}', 'target':f'p:{i}', 'text':'界'*4000} for i in range(3)]
    with pytest.raises(ValidationError): batch(large)


def test_closed_schema_and_exact_hash_preconditions():
    with pytest.raises(ValidationError): batch([{**patch(), 'script':'print(1)'}])
    with pytest.raises(ValidationError): batch([{**patch(), 'expected_hash':'guessed'}])
    with pytest.raises(ValidationError): batch([{**patch(), 'end':2}])
    with pytest.raises(ValidationError): batch([{**patch(), 'start':'4'}])


def test_reads_are_bounded_and_deduplicated():
    with pytest.raises(ValidationError): ReadObjects(path='a.docx',revision=HASH,ids=['p:1']*2)
    with pytest.raises(ValidationError): ReadObjects(path='a.docx',revision=HASH,ids=['p:1'],max_chars=99999)
    with pytest.raises(ValidationError): ListObjects(path='a.docx',revision=HASH,limit=500)
