"""Replay recorded planning/invalid creation offline; measure native repair prompt."""
import argparse,asyncio,json,os,sqlite3,tempfile
from pathlib import Path
from probe_word_repair_admission import sha,PIN
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.native_tools import render_prompt

async def main(args):
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    from transformers import AutoTokenizer
    report=json.loads(args.report.read_text());source=Path(report['project']);db=source/'.localoffice/state.sqlite3'
    before=sha(db);assert Path(report['model']['path']).name==PIN
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
        objective=conn.execute('SELECT objective FROM tasks WHERE id=?',(report['metrics']['task_id'],)).fetchone()[0]
        proposals=[r[0] for r in conn.execute("SELECT content FROM messages WHERE role='assistant' ORDER BY id")]
    creation=None
    for raw in proposals:
        parsed=json.loads(raw.removeprefix('<tool_call>').removesuffix('</tool_call>').strip())
        call=parsed if 'name' in parsed else {'name':parsed.get('tool'),'arguments':{k:v for k,v in parsed.items() if k!='tool'}}
        if call['name'] in ('word.create','word.prepare_creation'):creation=call;break
    assert creation and 'ct_' not in json.dumps(creation),'Probe requires unbound recorded creation'
    if args.creation_review:creation['name']='word.prepare_creation'
    planned=next(e['result'] for e in report['events'] if e.get('tool')=='word.plan_content' and e.get('result',{}).get('status')=='planned')
    literals=[{'role':item['role'],'quote':item['text']} for item in planned['literals']]
    assert all(item['source']=='objective' for item in planned['literals'])
    tokenizer=AutoTokenizer.from_pretrained(report['model']['path'],local_files_only=True,trust_remote_code=False)
    with tempfile.TemporaryDirectory(prefix='word-source-repair-admission-') as folder:
        store=ProjectStore(Path(folder))
        class Provider:
            native_tool_protocol=True;native_constrained=True;context_limit=4096
            def __init__(self):self.calls=0;self.prompts=[]
            def status(self):return {'loaded':True,'constrained_decoding':True}
            async def count_tokens(self,messages,*,tools):return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
            async def stream(self,messages,*,tools,**kwargs):
                self.calls+=1
                self.prompts.append({'tokens':await self.count_tokens(messages,tools=tools),
                    'tool_names':[t['function']['name'] for t in tools],'messages':messages})
                calls=[{'name':'word.plan_content','arguments':{'literals':literals}},
                       {'name':'word.load_tools','arguments':{'operations':['prepare_creation' if args.creation_review else 'create']}},creation,
                       {'name':'ask_user','arguments':{'text':'Offline admission probe complete.'}}]
                if args.replace_proposal or args.revise_proposal:
                    replacement=json.loads(json.dumps(creation))
                    replacement['arguments']['styles']=[{'id':'ReviewBand','font':'Arial','size_points':9}]
                    for band in ('header','footer'):replacement['arguments'][band]['style']='ReviewBand'
                    operation='revise_creation' if args.revise_proposal else 'prepare_creation'
                    if args.revise_proposal:
                        from localoffice.word.creation_repair import pending
                        task_id=store.db.execute('SELECT id FROM tasks').fetchone()[0]
                        state=pending(store,task_id)
                        # This branch is used only after the preparation call.
                        replacement={'name':'word.revise_creation','arguments':{'ref':state['ref'] if state else 'cr_'+'0'*32,
                            'changes':[{'op':'set','path':'styles','value':replacement['arguments']['styles']},
                                       {'op':'set','path':'header.style','value':'ReviewBand'},
                                       {'op':'set','path':'footer.style','value':'ReviewBand'}]}}
                    calls=calls[:3]+[{'name':'word.load_tools','arguments':{'operations':[operation]}},replacement,calls[-1]]
                assert self.calls<=len(calls)
                yield {'type':'token','text':'<tool_call>'+json.dumps(calls[self.calls-1],ensure_ascii=False)+'</tool_call>'}
                yield {'type':'finish','reason':'stop'}
        provider=Provider();runner=TaskRunner(store,provider,source_planning=True,creation_review=args.creation_review)
        try:
            task=await runner.start(objective,artifact=creation['arguments']['path'],mode='background');await runner.job
            reached=provider.calls==(6 if args.replace_proposal or args.revise_proposal else 4) and ('word.finalize_creation' if args.creation_review else 'word.repair_creation') in provider.prompts[-1]['tool_names']
            if args.replace_proposal or args.revise_proposal:
                reached=reached and sum(e.get('tool') in ('word.prepare_creation','word.revise_creation') and e.get('type')=='tool' for e in runner.events)==2
            result={'replacement_proposal':args.replace_proposal,'compact_revision':args.revise_proposal,'status':'admitted' if reached else 'not_reached','creation_review':args.creation_review,'report':str(args.report),'context_limit':4096,
                    'output_reserve':768,'safety_margin':128,'calls':provider.prompts,'events':runner.events,
                    'inference_performed':False,'weights_loaded':False,'source_database_unchanged':sha(db)==before,
                    'operations':store.db.execute('SELECT count(*) FROM operations').fetchone()[0]}
            assert result['operations']==0
            args.output.write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps({k:v for k,v in result.items() if k not in ('calls','events')}))
            print('prompt tokens:',[p['tokens'] for p in provider.prompts])
        finally:await runner.pause();store.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--creation-review',action='store_true');parser.add_argument('--replace-proposal',action='store_true');parser.add_argument('--revise-proposal',action='store_true')
    asyncio.run(main(parser.parse_args()))
