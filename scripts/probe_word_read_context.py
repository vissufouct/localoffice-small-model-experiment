"""Offline replay of actual read calls into a copied project; no inference."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from probe_word_repair_admission import ROOT, PIN, sha, clone_state, first_creation, rewind


async def main(report_path,output):
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.word.plugin import WordPlugin
    from localoffice.word.guidance import build_state,loaded_receipt
    from localoffice.native_history import record_call,record_result
    from localoffice.native_tools import render_prompt,validate_history
    from localoffice.errors import HarnessError
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    from transformers import AutoTokenizer
    report=json.loads(report_path.read_text());source=Path(report['project']);task=report['metrics']['task_id']
    database=source/'.localoffice/state.sqlite3'
    before={p.name:sha(p) for p in database.parent.glob('state.sqlite3*') if not p.name.endswith('-shm')}
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        boundary=first_creation(db,task)
        reads=[]
        for row in db.execute('SELECT function FROM native_calls WHERE task_id=? AND assistant_message>? ORDER BY assistant_message',(task,boundary['cutoff'])):
            call=json.loads(row[0])
            if call['name'] in ('word.find_text','word.list_objects','word.read_objects'):reads.append(call)
        objective=db.execute('SELECT objective FROM tasks WHERE id=?',(task,)).fetchone()[0]
    profile=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text())
    assert profile['revision']==PIN
    tokenizer=AutoTokenizer.from_pretrained(profile['local_path'],local_files_only=True,trust_remote_code=False)
    class Provider:
        native_tool_protocol=True;native_constrained=True;context_limit=4096
        def status(self):return {'loaded':True,'constrained_decoding':True}
        async def count_tokens(self,messages,*,tools):
            validate_history(messages);self.messages=messages;self.tools=tools
            return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    results=[]
    with tempfile.TemporaryDirectory(prefix='localoffice-read-context-probe-') as temporary:
        clone=Path(temporary);clone_state(source,clone)
        with sqlite3.connect(clone/'.localoffice/state.sqlite3') as db:rewind(db,task,boundary)
        store=ProjectStore(clone)
        try:
            owner=store.db.execute('SELECT chat_id FROM task_chats WHERE task_id=?',(task,)).fetchone()[0]
            with store.transaction():store.db.execute("UPDATE meta SET value=? WHERE key='active_chat'",(owner,))
            plugin=WordPlugin(store);footer=None
            # Reproduce the audited footer -> header -> header-root listing.
            for call in reads[:3]:
                identity=record_call(store,task,{'tool':call['name'],**call['arguments']})
                result=getattr(plugin,call['name'].split('.')[1])(**call['arguments'],task_id=task)
                record_result(store,task,identity,result)
                for obj in result.get('matches',[]):
                    if obj.get('story',{}).get('kind')=='footer':footer=obj
            assert footer is not None
            store.close();store=ProjectStore(clone)
            # Discard all old conversational history, as a strict continuation test.
            last=store.db.execute('SELECT max(id) FROM messages').fetchone()[0]
            with store.transaction():
                store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('history_floor:'+owner,str(last)))
            provider=Provider();runner=TaskRunner(store,provider,source_planning=True)
            state=build_state(['replace_text'])
            runner.native_call_id=record_call(store,task,{'tool':'word.load_tools','operations':['replace_text']})
            status='admitted';error=None
            try:await runner.assemble(task,word_tool_override=state,pending_tool_result=loaded_receipt(state))
            except HarnessError as exc:
                if exc.code!='context_capacity':raise
                status='rejected';error=str(exc)
            system=provider.messages[0]['content']
            exact=json.loads(system.split('Current authoritative state: ',1)[1].split('\nActive Word operations:',1)[0])
            cached=exact.get('current_read_context',{}).get('objects',[])
            item=next((x for x in cached if x['target']==footer['id']),None)
            assert item and item['expected_hash']==footer['evidence_ref']
            assert (item['start'],item['end'])==(footer['start'],footer['end'])
            assert item['text']==footer['text'] and not item['truncated']
            assert exact['objective']==objective
            assert store.candidate(boundary['path'])['candidate_hash']==boundary['candidate_revision']
            assert store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1
            results.append({'status':status,'error':error,'context':runner.context_info,
                            'footer_retained':True,'history_cleared':True,'system':system})
        finally:store.close()
    after={p.name:sha(p) for p in database.parent.glob('state.sqlite3*') if not p.name.endswith('-shm')}
    assert before==after
    result={'source_report':str(report_path),'source_database_unchanged':True,'weights_loaded':False,
            'inference_performed':False,'workers':'read-only calls in temporary copy','cases':results}
    output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':results[0]['status'],'input_tokens':results[0]['context']['input_tokens'],
                      'footer_retained':True,'source_unchanged':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();asyncio.run(main(args.report,args.output))
