"""Offline reconstruction of failed creation admission; copies state, loads tokenizer only."""
import asyncio,json,tempfile,shutil,sqlite3,copy,argparse
from pathlib import Path
from transformers import AutoTokenizer
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.native_tools import render_prompt
from localoffice.word.guidance import build_state,loaded_receipt

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True);parser.add_argument('--cutoff',type=int,default=6);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    report=json.loads(args.report.read_text());root=Path(tempfile.mkdtemp(prefix='creation-admission-probe-'));shutil.copytree(Path(report['project'])/'.localoffice',root/'.localoffice')
    store=ProjectStore(root);key=report['metrics']['task_id']
    with store.transaction():
        store.db.execute('DELETE FROM native_calls WHERE assistant_message>?',(args.cutoff,))
        store.db.execute('UPDATE native_calls SET result_message=NULL WHERE result_message>?',(args.cutoff,))
        store.db.execute('DELETE FROM message_chats WHERE message_id>?',(args.cutoff,))
        store.db.execute('DELETE FROM messages WHERE id>?',(args.cutoff,))
        store.db.execute('DELETE FROM meta WHERE key=?',('task_recovery:'+key,))
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())
    tokenizer=AutoTokenizer.from_pretrained(model['local_path'],local_files_only=True,trust_remote_code=False)
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def status(self):return {'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            self.messages=copy.deepcopy(messages);self.tools=copy.deepcopy(tools)
            return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    provider=Provider();runner=TaskRunner(store,provider,source_planning=True)
    call=store.db.execute('SELECT call_id FROM native_calls WHERE assistant_message=?',(args.cutoff,)).fetchone();runner.native_call_id=call[0]
    state=build_state(['create']);result={'source_report':str(args.report),'cutoff':args.cutoff,'weights_loaded':False,'inference_performed':False}
    try:
        await runner.assemble(key,word_tool_override=state,pending_tool_result=loaded_receipt(state));result['status']='admitted'
    except Exception as exc:result.update(status='rejected',error=str(exc))
    def count(messages,tools):return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    messages=provider.messages;tools=provider.tools
    result.update(context=runner.context_info,message_roles=[m['role'] for m in messages],input_tokens=count(messages,tools),system_plus_tools_tokens=count(messages[:1],tools),tools_only_tokens=count([{'role':'system','content':''}],tools),system_text=messages[0]['content'],last_exchange=messages[1:])
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k not in ('system_text','last_exchange','context')},indent=2));store.close()
if __name__=='__main__':asyncio.run(main())
