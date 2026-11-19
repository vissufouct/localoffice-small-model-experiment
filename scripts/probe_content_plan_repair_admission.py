"""Tokenizer-only admission of the actual rejected selection request; no weights."""
import argparse,asyncio,json,os,sqlite3,tempfile
from pathlib import Path
from benchmark_word_creation_cases import ROOT,PIN,sha

async def main(args):
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['TOKENIZERS_PARALLELISM']='false'
    report=json.loads(args.report.read_text());db=Path(report['project'])/'.localoffice/state.sqlite3'
    before=sha(db.read_bytes())
    conn=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
    try:
        calls=[json.loads(row[0]) for row in conn.execute('SELECT function FROM native_calls ORDER BY rowid')]
        call=next(c for c in calls if c['name']=='word.plan_content')
    finally:conn.close()
    from transformers import AutoTokenizer
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    assert approval['revision']==PIN
    tokenizer=AutoTokenizer.from_pretrained(approval['local_path'],local_files_only=True,trust_remote_code=False)
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def __init__(self):self.calls=0;self.counts=[];self.presented=[]
        def status(self):return {'loaded':True,'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            n=len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
            self.counts.append({'input_tokens':n,'tools':[x['function']['name'] for x in tools],
                                'messages':messages,'schemas':tools})
            return n
        async def stream(self,messages,*,tools,**kwargs):
            self.calls+=1
            self.presented.append({'step':self.calls,'tokens':await self.count_tokens(messages,tools=tools),
                                   'tools':[x['function']['name'] for x in tools]})
            assert self.calls<=(4 if args.defer_index is not None else 2)
            action={'name':'ask_user','arguments':{'text':'Offline probe terminator; not model behavior.'}}
            if self.calls==1:action=call
            elif args.defer_index is not None and self.calls==2:
                from localoffice.word.content_plan_repair import pending
                key=store.db.execute('SELECT id FROM tasks').fetchone()[0]
                action={'name':'word.repair_content_plan','arguments':{'ref':pending(store,key)['ref'],
                    'changes':[{'op':'defer_as_draft','index':args.defer_index}]}}
            elif args.defer_index is not None and self.calls==3:
                action={'name':'word.load_tools','arguments':{'operations':['prepare_creation']}}
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with tempfile.TemporaryDirectory(prefix='office-plan-repair-admission-') as tmp:
        store=ProjectStore(Path(tmp));provider=Provider()
        runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
        try:
            await runner.start(report['prompt'],artifact='report.docx',mode='background');await runner.job
            desired='word.prepare_creation' if args.defer_index is not None else 'word.repair_content_plan'
            admitted=provider.calls==(4 if args.defer_index is not None else 2) and desired in provider.presented[-1]['tools']
            result={'status':'admitted' if admitted else 'rejected','report':str(args.report),
                    'inference_performed':False,'weights_loaded':False,'scripted_defer_index':args.defer_index,
                    'context_limit':4096,'output_reserve':768,'safety_reserve':128,
                    'source_database_unchanged':sha(db.read_bytes())==before,'presented':provider.presented,
                    'count_trace':provider.counts,'events':runner.events,
                    'operations':store.db.execute('SELECT count(*) FROM operations').fetchone()[0]}
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:v for k,v in result.items() if k not in ('count_trace','events')}))
        finally:await runner.pause();store.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--defer-index',type=int,help='Explicit scripted diagnostic choice only; measure subsequent prepare admission.')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output already exists; retain historical evidence.')
    asyncio.run(main(args))
