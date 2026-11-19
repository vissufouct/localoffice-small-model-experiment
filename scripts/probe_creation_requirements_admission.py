"""Scripted requirement-selection diagnostic; pinned tokenizer only, never model evidence."""
import argparse
import asyncio
import copy
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from benchmark_word_creation_cases import ROOT,PIN,sha,runtime_sources


async def main(args):
    snapshot=runtime_sources()
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
    report=json.loads(args.report.read_text());source_db=Path(report['project'])/'.localoffice/state.sqlite3'
    db_hash=sha(source_db.read_bytes())
    with sqlite3.connect('file:'+str(source_db)+'?mode=ro',uri=True) as db:
        calls=[json.loads(r[0]) for r in db.execute('SELECT function FROM native_calls WHERE task_id=? ORDER BY rowid',(report['task_id'],))]
    select=next(c for c in calls if c['name']=='word.select_source_quotes')
    prepare=next(c for c in calls if c['name']=='word.prepare_creation')
    revision=next(c for c in calls if c['name']=='word.revise_creation')
    if 'ct_' in json.dumps(prepare):raise ValueError('Only observed plain-text proposal supported; no stale ct mapping.')
    # Deliberately scripted interpretations for this diagnostic, never inferred by runtime.
    requirements=[{'kind':'paragraph_words','source_quote':'8–16 words','paragraph_index':1,'min':8,'max':16},
                  {'kind':'page_geometry','source_quote':'one-inch margins','field':'margin','equals':1.0,'unit':'in'}]
    for item in requirements:
        if report['prompt'].count(item['source_quote'])!=1:raise ValueError('Diagnostic quote must occur exactly once.')
    requirements_source=None
    if args.requirements_report:
        if args.evidence_ranges:raise ValueError('Observed requirement replay must not transform evidence.')
        observed=json.loads(args.requirements_report.read_text())
        if observed['prompt']!=report['prompt']:raise ValueError('Observed requirements must have the same exact objective.')
        requirements_db=Path(observed['project'])/'.localoffice/state.sqlite3'
        requirements_db_hash=sha(requirements_db.read_bytes())
        with sqlite3.connect('file:'+str(requirements_db)+'?mode=ro',uri=True) as db:
            planned=[json.loads(row[0]) for row in db.execute('SELECT function FROM native_calls WHERE task_id=? ORDER BY rowid',(observed['task_id'],))]
        planned=[call for call in planned if call['name']=='word.plan_requirements']
        if len(planned)!=1:raise ValueError('Choose an observed task with exactly one requirement plan.')
        requirements=planned[0]['arguments']['requirements']
        requirements_source={'report':str(args.requirements_report),'arguments_unchanged':True}
    from transformers import AutoTokenizer
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text());assert approval['revision']==PIN
    tokenizer=AutoTokenizer.from_pretrained(approval['local_path'],local_files_only=True,trust_remote_code=False)
    from localoffice.storage import ProjectStore,digest
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    from localoffice.word.creation_repair import pending,get
    from localoffice.word.contracts import PrepareCreation
    from localoffice.word.requirement_checks import check
    from localoffice.errors import HarnessError
    if args.evidence_ranges:
        from localoffice.word.objective_evidence import catalogue_text
        catalogue=catalogue_text(report['prompt'])
        if not catalogue['available']:raise ValueError('Scripted range diagnostic requires complete catalogue.')
        for item in requirements:
            quote=item.pop('source_quote');start=report['prompt'].index(quote);end=start+len(quote)
            ids=[key for key,span in catalogue['segments'].items() if span['start']<end and span['end']>start]
            item['source_range']={'start':ids[0],'end':ids[-1]}
    phases=['source_selection','load_requirements','plan_requirements','load_prepare','prepare',
            'failing_finalization','revision_after_failure','post_revision']
    if args.automatic:
        phases=[phase for phase in phases if phase not in ('load_requirements','load_prepare')]
    if args.reload_review:
        phases.insert(phases.index('failing_finalization'),'load_finalize')
        phases.insert(phases.index('revision_after_failure'),'load_revision')
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def __init__(self):self.calls=0;self.trace=[];self.presented=[];self.actions=[];self.mappings=[]
        def status(self):return {'loaded':True,'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            n=len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
            self.trace.append({'before_scripted_step':self.calls+1,'input_tokens':n,
                'messages':copy.deepcopy(messages),'schemas':copy.deepcopy(tools)})
            return n
        async def stream(self,messages,*,tools,**kwargs):
            self.calls+=1;step=self.calls;phase=phases[step-1]
            names=[t['function']['name'] for t in tools]
            self.presented.append({'step':step,'phase':phases[step-1],
                'tokens':len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools))),'tools':names})
            action={'name':'ask_user','arguments':{'text':'Offline diagnostic terminator; not model behavior.'}}
            if phase=='source_selection':action=select
            elif phase=='load_requirements':action={'name':'word.load_tools','arguments':{'operations':['plan_requirements']}}
            elif phase=='plan_requirements':action={'name':'word.plan_requirements','arguments':{'source_hash':digest(report['prompt'].encode()),'requirements':requirements}}
            elif phase=='load_prepare':action={'name':'word.load_tools','arguments':{'operations':['prepare_creation']}}
            elif phase=='load_finalize':action={'name':'word.load_tools','arguments':{'operations':['finalize_creation']}}
            elif phase=='load_revision':action={'name':'word.load_tools','arguments':{'operations':['revise_creation']}}
            elif phase=='prepare':action=prepare
            elif phase in ('failing_finalization','revision_after_failure'):
                task_id=store.db.execute('SELECT task_id FROM native_calls ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                proposal=pending(store,task_id)
                if not proposal or proposal.get('status')!='prepared':raise ValueError('Fresh retained proposal required.')
                retained=get(store,task_id,proposal['ref'])
                expected=PrepareCreation.model_validate(prepare['arguments']).model_dump();expected['tool']='word.create'
                if retained['payload']!=expected:raise ValueError('Observed proposal changed before scripted no-op revision.')
                if check(store,task_id,retained['payload'])['status']!='mismatch':raise ValueError('Diagnostic must reject before worker.')
                if phase=='failing_finalization':
                    # No old ct bindings reused: plain literals remain exact.
                    action={'name':'word.finalize_creation','arguments':{'ref':proposal['ref'],'styles':[],'bindings':[]}}
                else:
                    action={'name':'word.revise_creation','arguments':{**revision['arguments'],'ref':proposal['ref']}}
                    self.mappings.append({'observed':revision['arguments']['ref'],'fresh':proposal['ref'],
                        'basis':'hash-verified retained exact observed preparation','changes_unchanged':action['arguments']['changes']==revision['arguments']['changes']})
            if action['name'] not in names:
                self.actions.append({'step':step,'not_exposed':action['name']})
                action={'name':'ask_user','arguments':{'text':'Offline diagnostic stopped: requested tool not admitted.'}}
            self.actions.append({'step':step,'action':action})
            yield {'type':'token','text':'<tool_call>'+json.dumps(action,ensure_ascii=False)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with tempfile.TemporaryDirectory(prefix='office-requirements-admission-') as tmp:
        store=ProjectStore(Path(tmp));provider=Provider();runner=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=args.automatic)
        worker_attempts=[]
        def prohibit_worker(*a,**kw):
            worker_attempts.append(True)
            raise HarnessError('probe_worker_forbidden','Tokenizer-only probe must never invoke a worker.')
        try:
            with patch('localoffice.word.plugin.WordPlugin._execute',prohibit_worker):
                await runner.start(report['prompt'],artifact=prepare['arguments']['path'],mode='background');await runner.job
            receipts=[{'function':json.loads(r[0]),'receipt':json.loads(r[1]) if r[1] else None}
                for r in store.db.execute('SELECT n.function,m.content FROM native_calls n LEFT JOIN messages m ON m.id=n.result_message ORDER BY n.rowid')]
            result={'diagnostic':'scripted selected requirements; not real-model interpretation or fixture acceptance',
                'source_report':str(args.report),'runtime_fingerprint':sha(json.dumps(snapshot,sort_keys=True).encode()),
                'runtime_sources_unchanged':runtime_sources()==snapshot,'source_database_unchanged':sha(source_db.read_bytes())==db_hash,
                'inference_performed':False,'weights_loaded':False,'automatic_routing_changed':args.automatic,
                'scripted_evidence_ranges':args.evidence_ranges,
                'observed_requirement_replay':requirements_source,
                'scripted_reload_review':args.reload_review,
                'requirements_source_database_unchanged':sha(requirements_db.read_bytes())==requirements_db_hash if requirements_source else None,
                'requirements':requirements,'observed_prepare':prepare,'fresh_reference_mappings':provider.mappings,
                'finalize_deviation':'scripted styles=[] bindings=[]; observed ct refs deliberately not replayed',
                'context_limit':4096,'output_reserve':768,'safety_reserve':128,'input_allowance':3200,
                'presented':provider.presented,'scripted_actions':provider.actions,'count_trace':provider.trace,
                'receipts':receipts,'events':runner.events,'worker_attempts':len(worker_attempts),
                'operations':store.db.execute('SELECT count(*) FROM operations').fetchone()[0],
                'all_phases_reached':provider.calls==len(phases)}
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:v for k,v in result.items() if k not in ('count_trace','receipts','events','observed_prepare','scripted_actions')},ensure_ascii=False))
        finally:await runner.pause();store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--automatic',action='store_true',help='Explicit experimental requirements routing; no inference.')
    parser.add_argument('--evidence-ranges',action='store_true',help='Scripted exact evidence ranges; not model-selected requirements.')
    parser.add_argument('--requirements-report',type=Path,help='Replay the exact single observed plan from a same-objective report; no inference.')
    parser.add_argument('--reload-review',action='store_true',help='Scripted single-tool loads before finalization and revision; no inference.')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output exists; retain evidence.')
    asyncio.run(main(args))
