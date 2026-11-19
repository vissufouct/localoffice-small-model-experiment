import copy
import json
import pytest
from pydantic import ValidationError
from localoffice.decoding import structural_schema
from localoffice.runtime import WORD_ACTION
from localoffice.word.guidance import project_schema


def test_structural_projection_preserves_contract_shape_without_mutating_host_schema():
    full=project_schema(WORD_ACTION.json_schema(),['format_table'])
    original=copy.deepcopy(full);small=structural_schema(full)
    assert full==original
    assert small['$defs']['FormatTable']['properties']['op']['const']=='format_table'
    assert small['$defs']['FormatTable']['additionalProperties'] is False
    assert small['$defs']['StageBatch']['required']==full['$defs']['StageBatch']['required']
    assert small['$defs']['StageBatch']['properties']['operations']['type']=='array'
    assert next(iter(small['$defs']['StageBatch']['properties']))=='tool'
    assert 'maxItems' not in small['$defs']['StageBatch']['properties']['operations']
    assert 'pattern' not in small['$defs']['FormatTable']['properties']['expected_hash']
    bad={'tool':'word.stage_batch','path':'report.docx','revision':'x','operations':[]}
    with pytest.raises(ValidationError):WORD_ACTION.validate_json(json.dumps(bad))


def test_structural_schemas_compile_without_large_finite_string_repetitions():
    core=pytest.importorskip('outlines_core.json_schema')
    for names in [[],['format_table'],['create'],['replace_text','set_list','set_style','format_paragraph']]:
        schema=structural_schema(project_schema(WORD_ACTION.json_schema(),names))
        regex=core.build_regex_from_schema(json.dumps(schema),whitespace_pattern='[ ]?')
        assert regex and '{0,200000}' not in regex and '{0,8000}' not in regex
