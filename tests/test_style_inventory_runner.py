"""Runner uses complete retained declarations, not truncated proposal summaries."""
import pytest

from localoffice.errors import HarnessError
from localoffice.native_tools import validate_native_arguments
from localoffice.runtime import TaskRunner
from localoffice.word.plugin import WordPlugin
from localoffice.word.guidance import build_state
from test_native_runner import NativeProvider
from test_word_content_plan import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('ids',[[],['Normal'],['Body'],[f'Body{i}' for i in range(16)]])
async def test_actual_runner_uses_current_explicit_styles(tmp_path,ids):
    store=fixture(tmp_path,'Create body text.')
    try:
        proposal=WordPlugin(store).prepare_creation(task_id='task',path='report.docx',
            styles=[{'id':name,'font':'Arial','size_points':12} for name in ids],
            blocks=[{'kind':'paragraph','text':'Body'}])
        if len(ids)==16:assert proposal['proposed_design']['omitted'] is True
        runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
        await runner.assemble('task',word_tool_override=build_state(['edit_creation_styles']))
        tool=next(t for t in runner.native_tools if t['function']['name']=='word.edit_creation_styles')
        schema=tool['function']['parameters']
        assert schema['properties']['ref']=={'const':proposal['ref']}
        def arguments(op,id):
            change={'op':op,'id':id,'size_points':12}
            if op=='declare':change['font']='Arial'
            return {'ref':proposal['ref'],'changes':[change]}
        # Declaration IDs remain model-selected; capacity stays host-validated.
        validate_native_arguments('word.edit_creation_styles',arguments('declare','NewStyle'),runner.native_tools)
        for name in ids:
            validate_native_arguments('word.edit_creation_styles',arguments('update',name),runner.native_tools)
        with pytest.raises(HarnessError):
            validate_native_arguments('word.edit_creation_styles',arguments('update','Absent'),runner.native_tools)
        if 'Normal' not in ids:
            with pytest.raises(HarnessError):
                validate_native_arguments('word.edit_creation_styles',arguments('update','Normal'),runner.native_tools)
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()
