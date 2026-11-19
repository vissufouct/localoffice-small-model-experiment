"""Inspect final Word context on a temporary project copy; no inference or workers."""
import argparse,asyncio,json,os,shutil,tempfile
from pathlib import Path
from benchmark_word_creation_cases import ROOT,PIN,sha,runtime_sources

async def main(args):
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['TOKENIZERS_PARALLELISM']='false'
    report=json.loads(args.report.read_text());origin=Path(report['project'])
    original_db=origin/'.localoffice/state.sqlite3';before=sha(original_db.read_bytes())
    source_snapshot=runtime_sources()
    from transformers import AutoTokenizer
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    assert approval['revision']==PIN
    tokenizer=AutoTokenizer.from_pretrained(approval['local_path'],local_files_only=True,trust_remote_code=False)
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    class Provider:
        native_tool_protocol=True;context_limit=4096
        def __init__(self):self.trace=[]
        def status(self):return {'loaded':True,'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            count=len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
            self.trace.append({'input_tokens':count,'tools':tools,'messages':messages})
            return count
    with tempfile.TemporaryDirectory(prefix='office-completion-admission-') as tmp:
        project=Path(tmp)/'project';shutil.copytree(origin,project)
        store=ProjectStore(project);provider=Provider()
        if args.before_terminal_error:
            # Reproduce assembly immediately after the last native result.
            # Only the temporary copy loses later terminal-error messages.
            boundary=store.db.execute('SELECT max(result_message) FROM native_calls WHERE task_id=?',
                                      (report['task_id'],)).fetchone()[0]
            if boundary is None:raise ValueError('No completed native call to locate the boundary.')
            rows=store.db.execute("SELECT id FROM messages WHERE task_id=? AND role='error' AND id>?",
                                  (report['task_id'],boundary)).fetchall()
            for row in rows:
                store.db.execute('DELETE FROM message_chats WHERE message_id=?',(row[0],))
                store.db.execute('DELETE FROM messages WHERE id=?',(row[0],))
        runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
        result={'inference_performed':False,'weights_loaded':False,'source_report':str(args.report),
                'before_terminal_error':args.before_terminal_error}
        try:
            await runner.assemble(report['task_id'])
            result['status']='admitted'
        except Exception as exc:
            result.update(status='rejected',error=str(exc))
        finally:
            result.update(count_trace=provider.trace,context_info=runner.context_info,
                runtime_sources_unchanged=runtime_sources()==source_snapshot,
                runtime_fingerprint=sha(json.dumps(source_snapshot,sort_keys=True).encode()),
                source_database_unchanged=sha(original_db.read_bytes())==before)
            store.close()
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k not in ('count_trace','context_info')}))
        print('Count trace:',[item['input_tokens'] for item in provider.trace])

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--before-terminal-error',action='store_true')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output exists; retain historical evidence.')
    asyncio.run(main(args))
