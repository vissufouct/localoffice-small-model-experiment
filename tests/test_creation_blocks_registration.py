from pathlib import Path
import pytest
from localoffice.errors import HarnessError
from localoffice.plugins import first_party_registry
from localoffice.word.contracts import EditCreationBlocks,LoadTools
from localoffice.word.guidance import MODELS,DIRECT_MODELS,prompt_schemas,GUIDES
from localoffice.word.plugin import WordPlugin
from localoffice.word import creation_blocks as blocks,creation_repair as repair
from test_creation_revision import context


def test_lazy_registration_full_fallback():
    assert MODELS['edit_creation_blocks'] is EditCreationBlocks
    assert DIRECT_MODELS['edit_creation_blocks'] is EditCreationBlocks
    assert LoadTools(operations=['edit_creation_blocks','revise_creation']).operations==['edit_creation_blocks','revise_creation']
    schemas=prompt_schemas(['edit_creation_blocks'])
    assert set(schemas)=={'word.edit_creation_blocks'}
    assert 'native_call_id' not in schemas['word.edit_creation_blocks']['properties']
    assert 'word.revise_creation' in prompt_schemas(['revise_creation'])
    assert 'revise_creation' in GUIDES['edit_creation_blocks'] and 'prepare_creation' in GUIDES['edit_creation_blocks']
    skill=Path('host/localoffice/builtin_skills/word-create/SKILL.md').read_text()
    assert 'word.edit_creation_blocks' in skill.split('---')[1]


@pytest.mark.asyncio
async def test_registered_tool_no_worker_and_disabled(context,monkeypatch):
    store,_,ref,_=context
    monkeypatch.setattr(WordPlugin,'_execute',lambda *a,**k:pytest.fail('Unexpected worker'))
    registry=first_party_registry(store)
    result=await registry.call('word.edit_creation_blocks',task_id='task',ref=ref,
                              changes=[{'op':'set_paragraph','index':0,'page_break_before':False}])
    assert result['ref']!=ref and repair.get(store,'task')['payload']['blocks'][0]['page_break_before'] is False
    registry.set_enabled('word',False)
    with pytest.raises(HarnessError,match='disabled'):
        registry.call('word.edit_creation_blocks',task_id='task',ref=result['ref'],changes=[{'op':'remove_block','index':1}])


def test_runtime_model_dump_preserves_sparse_properties():
    request=EditCreationBlocks(ref='cr_'+'a'*32,changes=[
        {'op':'set_paragraph','index':0,'page_break_before':False}])
    dumped=request.model_dump()
    assert dumped['changes']==[{'op':'set_paragraph','index':0,'page_break_before':False}]
    assert blocks.normalize(dumped)==dumped
    assert EditCreationBlocks.model_validate_json(request.model_dump_json()).model_dump()==dumped
