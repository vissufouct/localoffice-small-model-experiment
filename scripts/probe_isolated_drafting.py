"""One pinned native-model drafting diagnostic; no documents or acceptance claims."""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from benchmark_word_creation_cases import ROOT,PIN,runtime_sources,sha
from benchmark_word_body import prompt_for

async def run(output,source_span=None,production_context=False,planning_context=False):
    if output.exists():raise ValueError('Retain existing evidence; choose a new output.')
    if planning_context and (production_context or source_span is not None):raise ValueError('Planning diagnostic uses the complete objective and a separate mode.')
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    from localoffice.providers import MLXProvider
    from localoffice.native_tools import parse_qwen_call
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    model=approval['model']
    if approval['revision']!=PIN or Path(model['path']).name!=PIN or not Path(model['path']).is_dir():
        raise ValueError('Pinned local weights unavailable; no substitute/download.')
    objective=prompt_for();source=objective
    if source_span is not None:
        start,end=source_span
        if not 0<=start<end<=len(objective):raise ValueError('Invalid exact source span.')
        source=objective[start:end]
    messages=[{'role':'system','content':'Isolated drafting diagnostic: write only the newly requested paragraph. Copied passages and document formatting are handled separately. Call submit_draft with the actual paragraph text, or ask_user if essential information is missing. No file operations.'},
              {'role':'user','content':source}]
    tools=[{'type':'function','function':{'name':name,'parameters':{'type':'object','additionalProperties':False,
        'properties':{'text':{'type':'string','minLength':1,'maxLength':8000}},'required':['text']}}}
        for name in ('submit_draft','ask_user')]
    if production_context:
        from localoffice.word.draft_context import build
        messages,tools=build(source,'df_'+'0'*32)
    if planning_context:
        from localoffice.word.draft_context import planning_tool,build
        from localoffice.word.objective_evidence import catalogue_text
        catalogue=catalogue_text(objective)
        planning=planning_tool(catalogue)
        if planning is None:raise ValueError('Planning diagnostic needs a complete bounded catalogue.')
        tools=[planning,build('Draft','df_'+'0'*32)[1][1]]
        messages=[{'role':'system','content':
            'Select only the source instruction asking for newly written prose. Do not draft text or create a document. '
            'The user objective is reproduced completely in the catalogue. Select the smallest inclusive range containing '
            'the prose-writing request and its language, length, tone and meaning constraints. Exclude passages requested '
            'verbatim and separate layout/completion instructions. Call word.plan_draft with that range, or ask_user only '
            'if the prose-writing request is absent or genuinely ambiguous.'},
            {'role':'user','content':json.dumps(catalogue,ensure_ascii=False,separators=(',',':'))}]
    provider=MLXProvider(structural_decoding=False,native_tool_protocol=True,native_constrained=True,sampling_profile='low_temperature')
    snapshot=runtime_sources();started=time.monotonic()
    report={'diagnostic':'isolated drafting, not full task or document acceptance','model':model,'revision':PIN,
        'context_limit':4096,'output_reserve':768,'safety_reserve':128,'messages':messages,'tools':tools,
        'prompt_hash':sha(messages[-1]['content'].encode()),'source_objective':objective,
        'source_objective_hash':sha(objective.encode()),'source_span':source_span,
        'sampling':provider.sampling,'production_context_builder':production_context,'isolated_planning':planning_context,
        'synthetic_draft_ref':('df_'+'0'*32) if production_context else None,
        'runtime_fingerprint':sha(json.dumps(snapshot,sort_keys=True).encode()),'document_operations':0}
    try:
        await provider.start(model['path'],4096)
        count=await provider.count_tokens(messages,tools=tools);report['input_tokens']=count
        if count+896>4096:raise ValueError('Input does not fit unchanged budget.')
        raw='';events=[]
        async def generate():
            nonlocal raw
            async for event in provider.stream(messages,tools=tools,max_tokens=768):
                if event['type']=='token':raw+=event['text']
                else:events.append(event)
        await asyncio.wait_for(generate(),150)
        report.update(raw=raw,events=events,action=parse_qwen_call(raw,tools=tools),semantic_review='pending')
    except Exception as exc:report['error']=repr(exc)
    finally:
        await provider.stop()
        report.update(elapsed_seconds=time.monotonic()-started,runtime_sources_unchanged=runtime_sources()==snapshot)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:report.get(k) for k in ('action','error','input_tokens','elapsed_seconds','runtime_sources_unchanged')},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-span',type=int,nargs=2,metavar=('START','END'),help='Explicit exact source code-point span; diagnostic only, not automatic extraction.')
    parser.add_argument('--production-context',action='store_true',help='Use the current focused draft builder with a synthetic reference; no ledger or document execution.')
    parser.add_argument('--planning-context',action='store_true',help='Isolate source-instruction selection against the complete objective catalogue; no draft or document execution.')
    args=parser.parse_args();asyncio.run(run(args.output,args.source_span,args.production_context,args.planning_context))
