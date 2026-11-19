"""No-op capability routing preserves review and never executes finalization."""
import json
import pytest
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical,digest
from localoffice.word.creation_repair import capture,pending
from localoffice.word.creation_revision import revise
from localoffice.word.requirements import plan
from localoffice.word.guidance import build_state,selected
from test_word_content_plan import fixture

CHOICES=['edit_creation_blocks','revise_creation','finalize_creation']
OBJECTIVE='Use one-inch margins. Draft an invitation.'

class Pressure:
    native_tool_protocol=True
    context_limit=4096
    def __init__(self,fit):self.fit=fit;self.seen=[]
    def status(self):return {'loaded':True,'constrained_decoding':True}
    async def count_tokens(self,messages,*,tools):
        ops=json.loads(messages[0]['content'].split('Active Word operations: ',1)[1].split('\n',1)[0])['operations']
        self.seen.append(ops)
        return 3000 if ops==self.fit or not ops else 3500

@pytest.mark.asyncio
@pytest.mark.parametrize('condition',['pass','mismatch','empty','changed','initial'])
@pytest.mark.parametrize('fit',[['finalize_creation','revise_creation'],['finalize_creation']])
async def test_noop_complete_selected_pass_exposes_review_without_changing_document(tmp_path,condition,fit):
    store=fixture(tmp_path,OBJECTIVE);provider=Pressure(fit)
    runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        requirements=[] if condition=='empty' else [{'kind':'page_geometry','source_quote':'one-inch margins','field':'margin','equals':1.0,'unit':'in'}]
        plan(store,'task',source_hash=digest(OBJECTIVE.encode()),requirements=requirements)
        proposal=capture(store,'task',{'path':'report.docx','page':{'margin_twips':2880 if condition=='mismatch' else 1440},'blocks':[{'kind':'paragraph','text':'Draft'}]},allow_valid=True)
        if condition!='initial':
            revise(store,'task',proposal['ref'],[{'op':'set','path':'blocks.0.text','value':'Changed' if condition=='changed' else 'Draft'}])
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(CHOICES))))
        before=pending(store,'task')
        await runner.assemble('task')
        passing=condition in ('pass','changed','initial')
        assert selected(store,'task')['operations']==(fit if passing else [])
        assert pending(store,'task')==before
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert not (store.root/'report.docx').exists()
        if passing:
            assert 'Selected checks are not full acceptance' in runner.word_tool_state['context_recycled']
            loader=next(t['function'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
            assert 'revise_creation' in loader['parameters']['properties']['operations']['items']['enum']
        else:
            assert ['finalize_creation'] not in provider.seen
    finally:await runner.pause();store.close()
