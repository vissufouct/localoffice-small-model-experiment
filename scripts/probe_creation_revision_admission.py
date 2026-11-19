"""Tokenizer-only replay through the first successful generic revision; no inference or document creation."""
import argparse,asyncio,json,os,sqlite3,tempfile
from pathlib import Path
from benchmark_word_creation_cases import ROOT,PIN,sha,runtime_sources

async def main(args):
    source_snapshot=runtime_sources()
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['TOKENIZERS_PARALLELISM']='false'
    report=json.loads(args.report.read_text());db=Path(report['project'])/'.localoffice/state.sqlite3'
    before=sha(db.read_bytes())
    conn=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
    try:
        rows=[{'call_id':row[0],'action':json.loads(row[1]),
               'result':json.loads(row[2]) if row[2] else None}
              for row in conn.execute(
                  'SELECT n.call_id,n.function,m.content FROM native_calls n '
                  'LEFT JOIN messages m ON m.id=n.result_message '
                  'WHERE n.task_id=? ORDER BY n.rowid',(report['task_id'],))]
        revision_index=next(i for i,row in enumerate(rows)
            if row['action']['name']=='word.revise_creation'
            and isinstance(row['result'],dict)
            and row['result'].get('status') in ('prepared','invalid_design','missing_styles')
            and row['result'].get('document_changed') is False)
        prefix=rows[:revision_index+1]
        if [row['action']['name'] for row in prefix] != [
                'word.select_source_quotes','word.prepare_creation','word.revise_creation']:
            raise ValueError('Unsupported observed prefix; never skip intervening calls.')
        call,prepare,revision=[row['action'] for row in prefix]
        if 'ct_' in json.dumps(prepare):
            raise ValueError('Only observed plain-text proposals supported; no stale source-ref substitution.')
        old_ref=prefix[1]['result']['ref']
        if revision['arguments']['ref']!=old_ref:
            raise ValueError('Observed revision does not target the observed retained proposal.')
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
        def __init__(self):self.calls=0;self.counts=[];self.presented=[];self.ref_mapping=None;self.post_revision=None
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
            assert self.calls<=4
            action={'name':'ask_user','arguments':{'text':'Offline probe terminator; not model behavior.'}}
            if self.calls==1:action=call
            elif self.calls==2:action=prepare
            elif self.calls==3:
                from localoffice.word.creation_repair import get,pending
                from localoffice.word.contracts import PrepareCreation
                task_id=store.db.execute('SELECT task_id FROM native_calls ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                proposal=pending(store,task_id)
                if not proposal or proposal.get('status')!='prepared':
                    raise ValueError('Fresh prepared proposal required for explicit ref mapping.')
                retained=get(store,task_id,proposal['ref'])
                expected=PrepareCreation.model_validate(prepare['arguments']).model_dump()
                expected['tool']='word.create'
                if retained['payload']!=expected:
                    raise ValueError('Fresh retained proposal differs from observed submitted design.')
                self.ref_mapping={'observed':old_ref,'fresh':proposal['ref'],
                                  'basis':'fresh hash-verified retained exact proposal'}
                action={'name':revision['name'],'arguments':{
                    **revision['arguments'],'ref':proposal['ref']}}
                assert action['arguments']['changes']==revision['arguments']['changes']
            elif self.calls==4:
                row=store.db.execute('SELECT m.content FROM native_calls n JOIN messages m '
                    'ON m.id=n.result_message ORDER BY n.rowid DESC LIMIT 1').fetchone()
                self.post_revision=json.loads(row[0])
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with tempfile.TemporaryDirectory(prefix='office-creation-revision-admission-') as tmp:
        store=ProjectStore(Path(tmp));provider=Provider()
        runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
        try:
            await runner.start(report['prompt'],artifact='report.docx',mode='background');await runner.job
            expected_tools=['word.edit_creation_blocks','word.revise_creation','word.finalize_creation']
            admitted=provider.calls==4 and all(name in provider.presented[-1]['tools'] for name in expected_tools)
            revision_rows=store.db.execute('SELECT n.function,m.content FROM native_calls n '
                'JOIN messages m ON m.id=n.result_message ORDER BY n.rowid').fetchall()
            revision_receipts=[json.loads(content) for function,content in revision_rows
                               if json.loads(function)['name']=='word.revise_creation']
            operations=store.db.execute('SELECT count(*) FROM operations').fetchone()[0]
            admitted=admitted and operations==0 and len(revision_receipts)==1 and revision_receipts[0].get('status')=='prepared'
            result={'status':'admitted' if admitted else 'rejected','report':str(args.report),
                    'runtime_fingerprint':sha(json.dumps(source_snapshot,sort_keys=True).encode()),
                    'runtime_sources_unchanged':runtime_sources()==source_snapshot,
                    'inference_performed':False,'weights_loaded':False,'scripted_selection':True,
                    'scripted_prepare':True,'scripted_revision':True,
                    'observed_revision_call_id':prefix[2]['call_id'],
                    'exact_observed_changes':revision['arguments']['changes'],
                    'reference_mapping':provider.ref_mapping,'revision_receipts':revision_receipts,
                    'edit_effect_present':any('edit_effect' in r for r in revision_receipts),
                    'expected_next_operations':expected_tools,
                    'context_limit':4096,'output_reserve':768,'safety_reserve':128,
                    'source_database_unchanged':sha(db.read_bytes())==before,'presented':provider.presented,
                    'count_trace':provider.counts,'events':runner.events,
                    'operations':operations}
            result['qualified_post_effect_admission']=bool(admitted and result['edit_effect_present'] and result['runtime_sources_unchanged'] and result['source_database_unchanged'] and provider.presented[-1]['tokens']<=3200)
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:v for k,v in result.items() if k not in ('count_trace','events')}))
        finally:await runner.pause();store.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output already exists; retain historical evidence.')
    asyncio.run(main(args))
