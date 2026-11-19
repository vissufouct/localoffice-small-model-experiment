"""Offline post-creation admission: copied trial state, pinned tokenizer, no inference.

Run from the repository with .venv/bin/python scripts/probe_word_repair_admission.py
--report <trial.json> --output </tmp/result.json> [--compare </tmp/before.json>].
Runtime Python sources are snapshotted before import beside the output. Only the
clone is opened by ProjectStore; the original SQLite connection is read-only.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
PIN='50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def first_creation(connection,task_id):
    connection.row_factory=sqlite3.Row
    rows=connection.execute('SELECT n.*,m.content FROM native_calls n JOIN messages m ON m.id=n.result_message WHERE n.task_id=? ORDER BY n.assistant_message',(task_id,))
    for row in rows:
        call=json.loads(row['function']);receipt=json.loads(row['content'])
        if call['name'] not in ('word.create','word.repair_creation') or receipt.get('status')!='validated':continue
        operation=connection.execute('SELECT * FROM operations WHERE id=? AND task_id=?',(receipt.get('receipt_id'),task_id)).fetchone()
        if operation is None or operation['candidate_hash']!=receipt.get('candidate_revision'):
            raise ValueError('Creation receipt lacks matching operation evidence')
        return {'cutoff':row['result_message'],'call_id':row['call_id'],'function':call['name'],
                'operation_id':operation['id'],'candidate_revision':operation['candidate_hash'],'path':operation['path']}
    raise ValueError('No successful creation call/result in source trial')


def clone_state(source,destination):
    destination.mkdir(parents=True,exist_ok=True)
    shutil.copytree(source/'.localoffice',destination/'.localoffice',
                    ignore=shutil.ignore_patterns('state.sqlite3*','writer.lock'))
    with sqlite3.connect((source/'.localoffice/state.sqlite3').as_uri()+'?mode=ro',uri=True) as original:
        with sqlite3.connect(destination/'.localoffice/state.sqlite3') as cloned:original.backup(cloned)


def rewind(connection,task_id,boundary):
    """Drop later messages only; require creation is still the actual candidate."""
    candidate=connection.execute('SELECT operation_id,candidate_hash FROM candidates WHERE path=?',(boundary['path'],)).fetchone()
    if candidate is None or tuple(candidate)!=(boundary['operation_id'],boundary['candidate_revision']):
        raise ValueError('Later artifact mutation exists; cannot reconstruct by message cutoff alone')
    with connection:
        cutoff=boundary['cutoff']
        connection.execute('DELETE FROM native_calls WHERE assistant_message>?',(cutoff,))
        connection.execute('UPDATE native_calls SET result_message=NULL WHERE result_message>?',(cutoff,))
        connection.execute('DELETE FROM message_chats WHERE message_id>?',(cutoff,))
        connection.execute('DELETE FROM messages WHERE id>?',(cutoff,))
        connection.execute('DELETE FROM meta WHERE key IN (?,?)',('task_recovery:'+task_id,'history_floor:'+connection.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone()[0]))
        connection.execute("UPDATE tasks SET status='paused' WHERE id=?",(task_id,))


def runtime_snapshot(destination):
    target=destination/'host'/'localoffice'
    shutil.copytree(ROOT/'host'/'localoffice',target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    hashes={str(p.relative_to(destination)):sha(p) for p in sorted(target.rglob('*.py'))}
    # Import the exact snapshot, so concurrent production edits cannot mix code.
    if any(name=='localoffice' or name.startswith('localoffice.') for name in sys.modules):
        raise RuntimeError('Run the probe as a fresh process before importing localoffice')
    sys.path.insert(0,str(destination/'host'))
    return hashes


async def probe(args):
    report=json.loads(args.report.read_text());source=Path(report['project']).resolve();task_id=report['metrics']['task_id']
    if report['context']!=4096 or Path(report['model']['path']).name!=PIN:raise ValueError('Expected pinned Qwen4B 4096-context trial')
    output=args.output.resolve()
    if output==source or source in output.parents or ROOT/'host' in output.parents:raise ValueError('Output must not overwrite source project or runtime')
    output.parent.mkdir(parents=True,exist_ok=True)
    snapshot=Path(tempfile.mkdtemp(prefix=output.stem+'-sources-',dir=output.parent))
    hashes=runtime_snapshot(snapshot)
    from localoffice.storage import ProjectStore,canonical
    from localoffice.runtime import TaskRunner
    from localoffice.native_tools import render_prompt,validate_history
    from localoffice.native_history import record_call
    from localoffice.word.guidance import build_state,loaded_receipt,fingerprint
    from localoffice.errors import HarnessError
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    from transformers import AutoTokenizer
    model=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    tokenizer_path=Path(model['local_path'])
    if model['revision']!=PIN or tokenizer_path.name!=PIN:raise ValueError('Unexpected tokenizer revision')
    tokenizer=AutoTokenizer.from_pretrained(tokenizer_path,local_files_only=True,trust_remote_code=False)
    tokenizer_hashes={p.name:sha(p) for p in tokenizer_path.iterdir() if p.name in ('tokenizer.json','tokenizer_config.json','special_tokens_map.json','chat_template.jinja')}
    source_db=source/'.localoffice/state.sqlite3'
    source_hashes={p.name:sha(p) for p in source_db.parent.glob('state.sqlite3*') if not p.name.endswith('-shm')}
    with sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True) as original:
        boundary=first_creation(original,task_id)
        objective=original.execute('SELECT objective FROM tasks WHERE id=?',(task_id,)).fetchone()[0]
    def count(messages,tools):
        validate_history(messages)
        return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def __init__(self):self.trace=[];self.messages=[];self.tools=[]
        def status(self):return {'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            self.messages=copy.deepcopy(messages);self.tools=copy.deepcopy(tools)
            n=count(messages,tools)
            self.trace.append({'tokens':n,'roles':[m['role'] for m in messages],
                               'system_utf8_bytes':len(messages[0]['content'].encode())})
            return n
        async def stream(self,*args,**kwargs):raise AssertionError('Inference prohibited')
    results=[]
    for scenario,operations in [('load_replace_text',['replace_text']),('find_text_available',[])]:
        with tempfile.TemporaryDirectory(prefix='word-repair-admission-') as folder:
            clone=Path(folder);clone_state(source,clone)
            connection=sqlite3.connect(clone/'.localoffice/state.sqlite3')
            try:rewind(connection,task_id,boundary)
            finally:connection.close()
            store=ProjectStore(clone)
            try:
                owner=store.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task_id,)).fetchone()[0]
                with store.transaction():store.db.execute("UPDATE meta SET value=? WHERE key='active_chat'",(owner,))
                provider=Provider();runner=TaskRunner(store,provider,source_planning=report['source_planning'])
                state=build_state(operations) if operations else {'operations':[],'source_hash':fingerprint([]),'guidance':{},
                    'context_recycled':'Reload needed operations with word.load_tools before further editing.'}
                pending=None
                if operations:
                    runner.native_call_id=record_call(store,task_id,{'tool':'word.load_tools','operations':operations})
                    pending=loaded_receipt(state)
                status='admitted';error=None
                try:await runner.assemble(task_id,word_tool_override=state,pending_tool_result=pending)
                except HarnessError as exc:
                    if exc.code!='context_capacity':raise
                    status='rejected';error={'code':exc.code,'message':str(exc)}
                messages=provider.messages;tools=provider.tools;system=messages[:1]
                context=runner.context_info
                if context.get('output_reserve')!=768 or context.get('safety_margin')!=128:raise ValueError('Runtime reserve changed')
                exact_system=messages[0]['content'];objective_kept=objective in exact_system or canonical(objective)[1:-1] in exact_system
                if not objective_kept:raise ValueError('Full objective missing from assembled system')
                tool_names=[t['function']['name'] for t in tools]
                if 'word.find_text' not in tool_names:raise ValueError('Post-creation find_text is not exposed')
                total=count(messages,tools);system_tools=count(system,tools);empty_tools=count([{'role':'system','content':''}],tools)
                result={'scenario':scenario,'operations':operations,'status':status,'error':error,
                        'input_tokens':total,'system_plus_tools_tokens':system_tools,'tools_only_tokens':empty_tools,
                        'system_without_tools_tokens':count(system,[]),'latest_history_increment_tokens':total-system_tools,
                        'system_increment_over_empty_tools_tokens':system_tools-empty_tools,
                        'token_breakdown_note':'Template-measured differences, not independent additive tokenizer estimates.',
                        'context_limit':4096,'output_reserve':768,'safety_margin':128,'remaining_input_tokens':3200-total,
                        'full_objective_preserved':objective_kept,'find_text_registered_available':True,
                        'context':context,'count_trace':provider.trace,'tool_names':tool_names,
                        'system_text':exact_system,'retained_history':messages[1:],'tools':tools}
                results.append(result)
            finally:store.close()
    final_hashes={p.name:sha(p) for p in source_db.parent.glob('state.sqlite3*') if not p.name.endswith('-shm')}
    result={'source_report':str(args.report.resolve()),'source_project':str(source),'task_id':task_id,'boundary':boundary,
            'objective':objective,'weights_loaded':False,'inference_performed':False,'worker_executed':False,
            'tokenizer_revision':PIN,'tokenizer_hashes':tokenizer_hashes,'runtime_snapshot':str(snapshot),
            'runtime_source_hashes':hashes,'source_database_unchanged':source_hashes==final_hashes,'cases':results}
    if args.compare:
        before=json.loads(args.compare.read_text())
        if before['boundary']!=boundary or before['objective']!=objective:raise ValueError('Comparison has different source boundary/objective')
        result['comparison']=[{'scenario':case['scenario'],'before':old['input_tokens'],'after':case['input_tokens'],'delta':case['input_tokens']-old['input_tokens']} for case,old in zip(results,before['cases'])]
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(output),'boundary':boundary,'source_database_unchanged':result['source_database_unchanged'],
                      'cases':[{k:c[k] for k in ('scenario','status','input_tokens','system_plus_tools_tokens','tools_only_tokens','remaining_input_tokens')} for c in results]},indent=2))
    if not result['source_database_unchanged']:raise RuntimeError('Source database changed during probe; inspect concurrent activity')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--compare',type=Path)
    asyncio.run(probe(parser.parse_args()))


if __name__=='__main__':main()
