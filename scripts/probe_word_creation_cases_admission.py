"""Offline C1/C2 admission: local tokenizer, scripted plan/load, no proposals.

Never loads model weights, calls a document worker or renders. The scripted
ask_user terminator is probe control, not a fixture result or model behaviour.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from benchmark_word_creation_cases import ROOT,SPEC,PIN,CASES,prompt_for,sha,C1_LITERALS

OUTPUT=ROOT/'docs/benchmarks/word-creation-cases-admission-v1.json'
SOURCES=['host/localoffice/runtime.py','host/localoffice/native_tools.py','host/localoffice/native_history.py',
         'host/localoffice/word/guidance.py','host/localoffice/word/contracts.py','host/localoffice/word/content_plan.py',
         'host/localoffice/builtin_skills/word-create/SKILL.md','scripts/benchmark_word_creation_cases.py',
         'docs/benchmarks/word-creation-next-fixtures.md']


def selections(case):
    if case=='C1':
        return [{'role':'paragraph','quote':text} for text in C1_LITERALS]+[
            {'role':'header','quote':'Relatório de revisão'},{'role':'footer','quote':'Página '}]
    return [{'role':'paragraph','quote':f'Body page {n}'} for n in range(1,5)]+[
        item for label in ('First','Even','Default') for item in
        ({'role':'header','quote':label+' header'},{'role':'footer','quote':label+' page '})]


async def probe(case,tokenizer):
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def __init__(self):self.counts=[];self.presented=[];self.calls=0
        def status(self):return {'loaded':True,'constrained_decoding':True,'decoding_scope':'Offline scripted provider; tokenizer only, no model'}
        async def count_tokens(self,messages,*,tools):
            rendered=render_prompt(tokenizer,messages,tools=tools)
            count=len(tokenizer.encode(rendered))
            self.counts.append({'input_tokens':count,'fits_with_896_reserve':count+896<=4096,
                'tool_names':[t['function']['name'] for t in tools],
                'rendered_prompt_hash':sha(rendered.encode()),'messages':messages,'tools':tools})
            return count
        async def stream(self,messages,*,tools,**kwargs):
            self.calls+=1
            tokens=await self.count_tokens(messages,tools=tools)
            self.presented.append({'scripted_step':self.calls,'input_tokens':tokens,
                'tool_names':[t['function']['name'] for t in tools],
                'rendered_prompt_hash':self.counts[-1]['rendered_prompt_hash']})
            actions=[{'tool':'word.plan_content','literals':selections(case)},
                     {'tool':'word.load_tools','operations':['prepare_creation']},
                     {'tool':'ask_user','text':'Offline admission probe stops before submitting any proposal.'}]
            if self.calls>len(actions):raise RuntimeError('Probe exceeded its scripted boundary')
            action=actions[self.calls-1]
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],
                'arguments':{k:v for k,v in action.items() if k!='tool'}},ensure_ascii=False)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    with tempfile.TemporaryDirectory(prefix='word-creation-case-admission-') as folder:
        store=ProjectStore(Path(folder));provider=Provider()
        runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
        runner.max_steps=CASES[case]['steps']
        result={'fixture':case,'fixture_version':1,'prompt':prompt_for(case),'prompt_hash':CASES[case]['prompt_hash'],
                'scripted_selections':selections(case),'fixture_step_limit':CASES[case]['steps']}
        try:
            task=await runner.start(result['prompt'],artifact='report.docx',mode='live')
            await runner.job
            initial=provider.presented[0] if provider.presented else None
            loaded=next((p for p in provider.presented if p['scripted_step']==3 and 'word.prepare_creation' in p['tool_names']),None)
            attempts=[p for p in provider.counts if 'word.prepare_creation' in p['tool_names']]
            result.update(initial={'status':'admitted' if initial else 'rejected','input_tokens':initial['input_tokens'] if initial else None},
                loaded_prepare={'status':'admitted' if loaded else 'rejected',
                    'input_tokens':loaded['input_tokens'] if loaded else attempts[-1]['input_tokens'] if attempts else None,
                    'headroom_tokens':3200-loaded['input_tokens'] if loaded else None},
                scripted_presentations=provider.presented,count_trace=provider.counts,events=runner.events,
                final_context=runner.context_info,
                operations=store.db.execute('SELECT count(*) FROM operations').fetchone()[0],
                proposals=store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'word_creation_repair:%'").fetchone()[0],
                candidate_present=store.candidate('report.docx') is not None)
            assert result['operations']==result['proposals']==0 and not result['candidate_present']
        finally:
            await runner.pause();store.close()
        return result


async def main():
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1';os.environ['TOKENIZERS_PARALLELISM']='false'
    from transformers import AutoTokenizer
    approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    path=Path(approval['local_path'])
    assert approval['revision']==PIN and path.name==PIN
    before={name:sha((ROOT/name).read_bytes()) for name in SOURCES}
    tokenizer=AutoTokenizer.from_pretrained(str(path),local_files_only=True,trust_remote_code=False)
    report={'kind':'scripted_offline_tokenizer_admission','context_limit':4096,'input_limit':3200,
        'output_reserve':768,'safety_reserve':128,'tokenizer_revision':PIN,
        'tokenizer_hashes':{p.name:sha(p.read_bytes()) for p in path.iterdir() if p.name in
            ('tokenizer.json','tokenizer_config.json','special_tokens_map.json','chat_template.jinja')},
        'weights_loaded':False,'inference_performed':False,'worker_invoked':False,'rendering_performed':False,
        'proposal_submitted':False,'runtime_sources_before':before,'cases':[],
        'limitations':['Exact literal planning choices and tool loading were scripted, not model-generated.',
            'Only initial and loaded prepare_creation admission measured; no proposal/output decoding, later context or task correctness qualified.',
            'The third scripted response pauses the temporary task; it is not a fixture clarification failure or model acceptance result.']}
    try:
        for case in CASES:report['cases'].append(await probe(case,tokenizer))
    finally:
        report['runtime_sources_after']={name:sha((ROOT/name).read_bytes()) for name in SOURCES}
        report['runtime_sources_unchanged']=report['runtime_sources_before']==report['runtime_sources_after']
        OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'report':str(OUTPUT),'runtime_sources_unchanged':report['runtime_sources_unchanged'],
        'cases':[{k:r[k] for k in ('fixture','initial','loaded_prepare','operations','proposals')} for r in report['cases']]},indent=2))


if __name__=='__main__':asyncio.run(main())
