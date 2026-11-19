"""Pinned real-model repair after a SCRIPTED recorded prefix; not full-task acceptance."""
import argparse,asyncio,json,sqlite3,tempfile,time,io
from pathlib import Path
from probe_word_repair_admission import PIN,sha
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

async def main(args):
    recorded=json.loads(args.report.read_text());source=Path(recorded['project']);db=source/'.localoffice/state.sqlite3';before=sha(db)
    assert Path(recorded['model']['path']).name==PIN
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
        objective=conn.execute('SELECT objective FROM tasks WHERE id=?',(recorded['metrics']['task_id'],)).fetchone()[0]
        raws=[r[0] for r in conn.execute("SELECT content FROM messages WHERE role='assistant' ORDER BY id")]
    creation=None
    for raw in raws:
        if raw.startswith('<tool_call>'):
            parsed=json.loads(raw.removeprefix('<tool_call>').removesuffix('</tool_call>').strip())
            if parsed['name']=='word.create':creation=parsed;break
    assert creation and 'ct_' not in json.dumps(creation)
    planned=next(e['result'] for e in recorded['events'] if e.get('tool')=='word.plan_content' and e.get('result',{}).get('status')=='planned')
    assert all(x['source']=='objective' for x in planned['literals'])
    prefix=[{'name':'word.plan_content','arguments':{'literals':[{'role':x['role'],'quote':x['text']} for x in planned['literals']]}},
            {'name':'word.load_tools','arguments':{'operations':['create']}},creation]
    real=MLXProvider(native_tool_protocol=True,native_constrained=True,sampling_profile='low_temperature')
    class DiagnosticProvider:
        def __init__(self):self.scripted_calls=0;self.real_calls=0
        def __getattr__(self,name):return getattr(real,name)
        async def count_tokens(self,messages,**kwargs):return await real.count_tokens(messages,**kwargs)
        async def stream(self,messages,**kwargs):
            if self.scripted_calls<len(prefix):
                call=prefix[self.scripted_calls];self.scripted_calls+=1
                yield {'type':'token','text':'<tool_call>'+json.dumps(call,ensure_ascii=False)+'</tool_call>'}
                yield {'type':'finish','reason':'stop'}
            else:
                self.real_calls+=1
                async for event in real.stream(messages,**kwargs):yield event
    provider=DiagnosticProvider();root=Path(tempfile.mkdtemp(prefix='localoffice-source-repair-diagnostic-'))
    store=ProjectStore(root);runner=TaskRunner(store,provider,source_planning=True);started=time.monotonic()
    result={'scope':'Diagnostic only: three scripted prefix calls, then pinned real model. Not complete-task/model acceptance.',
            'source_report':str(args.report),'project':str(root),'model':recorded['model'],'context':4096,'sampling':real.sampling}
    try:
        await real.start(recorded['model']['path'],4096)
        task=await runner.start(objective,artifact='report.docx',mode='live');await asyncio.wait_for(runner.job,150)
        result['task_status']=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        result['metrics']=summary(store,task);result['events']=runner.events
        result['source_correction_observed']=False
        candidate=store.candidate('report.docx')
        if candidate:
            from localoffice.word.content_coverage import check
            result['content_coverage']=check(store,task)
            result['source_correction_observed']=bool(result['content_coverage']['matched']) and not result['content_coverage']['pending']
        result['explicit_binding_calls']=[json.loads(r[0]) for r in store.db.execute('SELECT function FROM native_calls WHERE task_id=?',(task,))
            if json.loads(r[0]).get('name')=='word.repair_creation' and (json.loads(r[0])['arguments'].get('source_bindings') or json.loads(r[0])['arguments'].get('bindings'))]
    except Exception as exc:result['error']=repr(exc)
    finally:
        await runner.pause();await real.close();store.close()
        result.update(scripted_prefix_calls=provider.scripted_calls,real_model_calls=provider.real_calls,
                      elapsed_seconds=time.monotonic()-started,source_database_unchanged=sha(db)==before)
        args.output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k not in ('metrics','events','model','content_coverage')}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args()))
