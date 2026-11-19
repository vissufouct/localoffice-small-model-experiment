"""Pinned-tokenizer admission regression. Offline; no inference or weight loading."""
import json
from pathlib import Path
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.native_tools import render_prompt,validate_history
from localoffice.native_history import record_call,record_result
from localoffice.word.content_plan import plan
from localoffice.word.guidance import build_state,loaded_receipt

PROMPT='Create report.docx with exactly two body paragraphs: First page and Second page. Put a page break before Second page. Add a right-aligned header Quarterly report and a centered footer showing Page followed by the current page number, of, and total pages, using real page-number fields. Use A4 portrait, Arial 11 point body and 9 point header/footer. Stage the document and finish.'

@pytest.mark.asyncio
async def test_four_source_references_and_full_create_fit_pinned_4k_context(tmp_path):
    from transformers import AutoTokenizer
    profile=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())
    if not Path(profile['local_path']).is_dir():pytest.skip('Pinned local tokenizer unavailable; no download allowed')
    tokenizer=AutoTokenizer.from_pretrained(profile['local_path'],local_files_only=True,trust_remote_code=False)
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def status(self):return {'loaded':True,'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            validate_history(messages)
            return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider(),source_planning=True)
    async def no_inference(*args):pass
    runner.run=no_inference
    try:
        key=await runner.start(PROMPT,artifact='report.docx',mode='background');await runner.job
        literals=[{'role':role,'quote':quote} for role,quote in [('paragraph','First page'),('paragraph','Second page'),('header','Quarterly report'),('footer','Page ')]]
        call=record_call(store,key,{'tool':'word.plan_content','literals':literals})
        planned=plan(store,key,literals=literals);record_result(store,key,call,planned)
        runner.native_call_id=record_call(store,key,{'tool':'word.load_tools','operations':['create']})
        state=build_state(['create'])
        messages=await runner.assemble(key,word_tool_override=state,pending_tool_result=loaded_receipt(state))
        assert runner.context_info['input_tokens']+896<=4096
        assert runner.context_info['output_reserve']==768 and runner.context_info['safety_margin']==128
        assert any(m['role']=='user' and m['content']==PROMPT for m in messages)
        for literal in planned['literals']:assert literal['ref'] in messages[0]['content'] and literal['text'] in messages[0]['content']
        params=next(t['function']['parameters'] for t in runner.native_tools if t['function']['name']=='word.create')
        assert {'styles','page','blocks','header','footer'}<=params['properties'].keys()
        assert 'literal_refs' not in params['properties']
        # Native creation uses inline references; the host still supports the
        # historical map. Both paths are covered in test_native_creation_sources.
        assert 'LiteralTextReference' in params['$defs']
        assert params['properties']['blocks']['maxItems']==24
        assert store.db.execute('SELECT count(*) FROM word_content_refs WHERE task_id=?',(key,)).fetchone()[0]==4
        assert not store.candidate('report.docx')
    finally:await runner.pause();store.close()
