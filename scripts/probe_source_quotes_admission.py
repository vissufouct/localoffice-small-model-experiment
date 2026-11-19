"""Tokenizer-only replay of an observed quote selection; no inference or document creation."""
import argparse,asyncio,json,os,sqlite3,tempfile
from pathlib import Path
from benchmark_word_creation_cases import ROOT,PIN,sha,runtime_sources

async def main(args):
    if args.review_tools and not args.through_prepare:
        raise ValueError('--review-tools requires --through-prepare.')
    source_snapshot=runtime_sources()
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['TOKENIZERS_PARALLELISM']='false'
    report=json.loads(args.report.read_text());db=Path(report['project'])/'.localoffice/state.sqlite3'
    before=sha(db.read_bytes())
    conn=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
    try:
        calls=[json.loads(row[0]) for row in conn.execute('SELECT function FROM native_calls ORDER BY rowid')]
        call=next(c for c in calls if c['name']=='word.select_source_quotes')
        prepare=next((c for c in calls if c['name']=='word.prepare_creation'),None) if args.through_prepare else None
        if args.through_prepare:
            if prepare is None or 'ct_' in json.dumps(prepare):
                raise ValueError('Prepare replay requires an observed plain-text proposal; stale references are never substituted.')
    finally:conn.close()
    from transformers import AutoTokenizer
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    assert approval['revision']==PIN
    tokenizer=AutoTokenizer.from_pretrained(approval['local_path'],local_files_only=True,trust_remote_code=False)
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    class ProbeRunner(TaskRunner):
        async def assemble(self,key,*,word_tool_override=None,pending_tool_result=None):
            if args.review_tools and word_tool_override is None:
                from localoffice.word.creation_repair import pending
                from localoffice.word.guidance import build_state
                proposal=pending(self.store,key)
                if proposal and proposal.get('status')=='prepared':
                    word_tool_override=build_state(args.review_tools)
            return await super().assemble(key,word_tool_override=word_tool_override,
                                          pending_tool_result=pending_tool_result)
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
            assert self.calls<=(3 if args.through_prepare else 2)
            action={'name':'ask_user','arguments':{'text':'Offline probe terminator; not model behavior.'}}
            if self.calls==1:action=call
            elif self.calls==2 and args.through_prepare:action=prepare
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with tempfile.TemporaryDirectory(prefix='office-source-quotes-admission-') as tmp:
        store=ProjectStore(Path(tmp));provider=Provider()
        runner=ProbeRunner(store,provider,source_planning=True,creation_review=True)
        try:
            await runner.start(report['prompt'],artifact='report.docx',mode='background');await runner.job
            desired=('word.'+args.expected_next if args.through_prepare else 'word.prepare_creation')
            expected_tools=['word.'+name for name in args.review_tools] if args.review_tools else [desired]
            admitted=provider.calls==(3 if args.through_prepare else 2) and all(name in provider.presented[-1]['tools'] for name in expected_tools)
            result={'status':'admitted' if admitted else 'rejected','report':str(args.report),
                    'runtime_fingerprint':sha(json.dumps(source_snapshot,sort_keys=True).encode()),
                    'runtime_sources_unchanged':runtime_sources()==source_snapshot,
                    'inference_performed':False,'weights_loaded':False,'scripted_selection':True,
                    'scripted_prepare':args.through_prepare,
                    'review_tools_override':args.review_tools or None,
                    'review_override_scope':'fresh prepared proposal only' if args.review_tools else None,
                    'expected_next_operation':desired,
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
    parser.add_argument('--through-prepare',action='store_true',help='Replay an observed plain-text proposal too; stop before finalization.')
    parser.add_argument('--expected-next',choices=('finalize_creation','revise_creation','edit_creation_blocks'),default='finalize_creation',help='Expected exposed operation after a scripted proposal; no edits are executed.')
    parser.add_argument('--review-tools',nargs='+',choices=('edit_creation_blocks','revise_creation','finalize_creation'),help='Offline-only tool override after fresh prepared state; original default unchanged.')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output already exists; retain historical evidence.')
    asyncio.run(main(args))
