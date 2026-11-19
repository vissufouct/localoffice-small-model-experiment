"""Offline native prompt admission with the installed tokenizer; no weights loaded."""
import asyncio,json,tempfile,sys
from pathlib import Path
from transformers import AutoTokenizer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_tables import saved_table
from test_word_progress import task
from localoffice.runtime import TaskRunner
from localoffice.native_tools import render_prompt
from localoffice.word.guidance import build_state

async def main():
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())
    tokenizer=AutoTokenizer.from_pretrained(model['local_path'],local_files_only=True,trust_remote_code=False)
    class Provider:
        native_tool_protocol=True
        context_limit=4096
        def status(self):return {'constrained_decoding':False}
        async def count_tokens(self,messages,*,tools):
            return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    root=Path(tempfile.mkdtemp(prefix='native-context-probe-'))
    store,plugin,original,revision=saved_table(root);task(store)
    runner=TaskRunner(store,Provider());report={'weights_loaded':False,'inference_performed':False,'cases':[]}
    try:
        for operations in ([],['format_text'],['create_character_style']):
            try:
                await runner.assemble('task',word_tool_override=build_state(operations) if operations else {'operations':[],'guidance':{}})
                report['cases'].append({'operations':operations,'status':'admitted','context':runner.context_info})
            except Exception as exc:
                report['cases'].append({'operations':operations,'status':'failed','error':str(exc),'context':runner.context_info})
    finally:store.close()
    Path('docs/benchmarks/native-context-admission.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps([{'operations':r['operations'],'status':r['status'],'tokens':r['context'].get('input_tokens'),'error':r.get('error')} for r in report['cases']],indent=2))

if __name__=='__main__':asyncio.run(main())
