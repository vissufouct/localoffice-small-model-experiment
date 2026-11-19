"""Pinned-tokenizer, scripted focused-draft handoff; never model accuracy evidence."""
import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from benchmark_word_creation_cases import ROOT, PIN, sha, runtime_sources


async def run(args):
    snapshot=runtime_sources()
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
    report=json.loads(args.report.read_text())
    source_db=Path(report['project'])/'.localoffice/state.sqlite3'
    source_hash=sha(source_db.read_bytes())
    with sqlite3.connect('file:'+str(source_db)+'?mode=ro',uri=True) as db:
        calls=[json.loads(r[0]) for r in db.execute('SELECT function FROM native_calls WHERE task_id=? ORDER BY rowid',(report['task_id'],))]
    select=next(c for c in calls if c['name']=='word.select_source_quotes')
    requirements=next(c for c in calls if c['name']=='word.plan_requirements')
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    assert approval['revision']==PIN
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(approval['local_path'],local_files_only=True,trust_remote_code=False)
    from localoffice.storage import ProjectStore,digest
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    from localoffice.word.objective_evidence import catalogue_text
    catalogue=catalogue_text(report['prompt'])
    # Explicit diagnostic selection, not inferred runtime behavior.
    instruction='second, draft one neutral English sentence of 8–16 words inviting the reader to review the document, without claiming a review has already happened or adding factual claims about an organisation'
    begin=report['prompt'].index(instruction);end=begin+len(instruction)
    ids=[key for key,span in catalogue['segments'].items() if span['start']<end and span['end']>begin]
    selected={'source_hash':digest(report['prompt'].encode()),'source_range':{'start':ids[0],'end':ids[-1]}}
    phases=['source_selection','requirements','draft_plan','draft_submit','parent_return']
    class Provider:
        native_tool_protocol=True
        native_constrained=True
        context_limit=4096
        sampling={'temperature':0.2,'top_p':1.0,'top_k':0,'min_p':0.0}
        def __init__(self):self.trace=[];self.presented=[]
        def status(self):return {'loaded':True,'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            n=len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
            self.trace.append(n)
            return n
        async def stream(self,messages,*,tools,**kwargs):
            step=len(self.presented)
            names=[t['function']['name'] for t in tools]
            self.presented.append({'phase':phases[step],'tokens':self.trace[-1],'tools':names})
            if step==0:action=select
            elif step==1:action=requirements
            elif step==2:action={'name':'word.plan_draft','arguments':selected}
            elif step==3:
                tool=next(t for t in tools if t['function']['name']=='word.submit_draft')
                action={'name':'word.submit_draft','arguments':{'ref':tool['function']['parameters']['properties']['ref']['const'],
                    'text':'Please take a moment to review the document for accuracy and clarity.'}}
            else:action={'name':'ask_user','arguments':{'text':'Scripted tokenizer diagnostic complete; no model inference.'}}
            if action['name'] not in names:
                action={'name':'ask_user','arguments':{'text':'Scripted diagnostic stopped: requested tool unavailable.'}}
            yield {'type':'token','text':'<tool_call>'+json.dumps(action,ensure_ascii=False)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with tempfile.TemporaryDirectory(prefix='office-focused-admission-') as directory:
        store=ProjectStore(Path(directory));provider=Provider()
        runner=TaskRunner(store,provider,source_planning=True,creation_review=True,requirements_planning=True,focused_drafting=True,isolated_draft_planning=args.isolated_draft_planning)
        attempted=[]
        def forbidden(*a,**kw):
            attempted.append(True)
            raise RuntimeError('No worker permitted in tokenizer diagnostic')
        try:
            with patch('localoffice.word.plugin.WordPlugin._execute',forbidden):
                await runner.start(report['prompt'],artifact='report.docx',mode='background');await runner.job
            result={'diagnostic':'scripted focused-draft context handoff; not model behavior or acceptance',
                    'inference_performed':False,'isolated_draft_planning':args.isolated_draft_planning,'weights_loaded':False,'source_report':str(args.report),
                    'source_database_unchanged':sha(source_db.read_bytes())==source_hash,
                    'runtime_sources_unchanged':runtime_sources()==snapshot,
                    'runtime_source_hashes':snapshot,'model_revision':PIN,
                    'scripted_selection':selected,'scripted_instruction_span':[begin,end],
                    'input_allowance':3200,'presented':provider.presented,'count_trace':provider.trace,
                    'events':runner.events,'all_phases_reached':len(provider.presented)==len(phases),
                    'prepare_tool_admitted':bool(provider.presented and provider.presented[-1]['phase']=='parent_return' and 'word.prepare_creation' in provider.presented[-1]['tools']),
                    'worker_attempts':len(attempted),'operations':store.db.execute('SELECT count(*) FROM operations').fetchone()[0]}
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:result[k] for k in ('all_phases_reached','presented','worker_attempts','operations','runtime_sources_unchanged')},ensure_ascii=False))
        finally:await runner.pause();store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--isolated-draft-planning',action='store_true')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output exists; retain evidence.')
    asyncio.run(run(args))
