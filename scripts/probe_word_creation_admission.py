"""Offline native-create context admission on a copied pre-creation task."""
import argparse,asyncio,json,os,sqlite3,tempfile
from pathlib import Path
from probe_word_repair_admission import clone_state,sha,PIN,ROOT
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.native_tools import render_prompt
from localoffice.word.guidance import build_state
from localoffice.errors import HarnessError

async def main(args):
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    from transformers import AutoTokenizer
    report=json.loads(args.report.read_text());source=Path(report['project'])
    assert report['context']==4096 and Path(report['model']['path']).name==PIN
    tokenizer=AutoTokenizer.from_pretrained(report['model']['path'],local_files_only=True,trust_remote_code=False)
    db=source/'.localoffice/state.sqlite3';before=sha(db)
    with tempfile.TemporaryDirectory(prefix='word-create-admission-') as folder:
        root=Path(folder);clone_state(source,root);store=ProjectStore(root)
        if args.cutoff_message is not None:
            with store.transaction():
                cutoff=args.cutoff_message
                store.db.execute('DELETE FROM native_calls WHERE assistant_message>?',(cutoff,))
                store.db.execute('UPDATE native_calls SET result_message=NULL WHERE result_message>?',(cutoff,))
                store.db.execute('DELETE FROM message_chats WHERE message_id>?',(cutoff,))
                store.db.execute('DELETE FROM messages WHERE id>?',(cutoff,))
        class Provider:
            native_tool_protocol=True;native_constrained=True;context_limit=4096
            def __init__(self):self.counts=[]
            def status(self):return {'constrained_decoding':True}
            async def count_tokens(self,messages,*,tools):
                count=len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
                self.counts.append(count);return count
        provider=Provider();runner=TaskRunner(store,provider,source_planning=report.get('source_planning',False))
        try:
            task=report['metrics']['task_id'];assert store.candidate('report.docx') is None
            try:
                messages=await runner.assemble(task,word_tool_override=build_state(['create']))
                result={'status':'admitted','context':runner.context_info,'retained_non_system_messages':messages[1:]}
            except HarnessError as exc:result={'status':'rejected','error':str(exc),'code':exc.code}
            result.update(report=str(args.report),counts=provider.counts,weights_loaded=False,inference_performed=False,
                          source_database_unchanged=sha(db)==before,cutoff_message=args.cutoff_message)
            args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
        finally:store.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--cutoff-message',type=int)
    asyncio.run(main(parser.parse_args()))
