"""Deterministic visual fixture for explicit list restart and continuation.

Run only after the coordinator's worker build slot. Does not qualify model use.
"""
import argparse
import asyncio
import io
import json
import sys
from pathlib import Path
from pypdf import PdfReader
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_lists import saved_table,get,restart_list,apply_list
from localoffice.word.render import WordRenderer

async def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    store,plugin,original,revision=saved_table(root)
    try:
        first=restart_list(plugin,revision,get(plugin,revision,'paragraph:2'),7)
        new_id=first['results'][0]['number_id']
        second=apply_list(plugin,first['candidate_revision'],get(plugin,first['candidate_revision'],'paragraph:3'),new_id,key='continue-new-list')
        final=second['candidate_revision'];data=store.blob(final)
        result=await WordRenderer(store).render(path='report.docx',revision=final)
        pdf=store.blob(result['pdf_hash']);reader=PdfReader(io.BytesIO(pdf))
        (root/'candidate.docx').write_bytes(data);(root/'preview.pdf').write_bytes(pdf)
        text=' '.join(reader.pages[0].extract_text().split())
        assert '7. Evidence-backed introduction.' in text,text
        assert '8. First item' in text,text
        assert '1. Numbered item' in text,text
        assert len(reader.pages)==2 and 'Next page' in reader.pages[1].extract_text()
        assert (root/'report.docx').read_bytes()==original
        store.apply(second['receipt_id'],expected_candidate=final,action='apply')
        undo=store.undo(second['receipt_id'],'undo-restart-batch');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==original
        restore=store.undo(undo['id'],'restore-restart-batch');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='apply')
        assert (root/'report.docx').read_bytes()==data
        evidence={'status':'rendered_pending_inspection','revision':final,'pdf_hash':result['pdf_hash'],'pages':len(reader.pages),
                  'new_number_id':new_id,'restart_at':7,'rendered_sequences':['7. Evidence-backed introduction.','8. First item','1. Numbered item'],
                  'exact_undo_restore':True,'original_unchanged_before_apply':True,'reviewer':None,'viewed_pages':[],
                  'limitation':'Registered deterministic worker operations; real-model list restart not qualified.'}
        (root/'evidence.json').write_text(json.dumps(evidence,indent=2)+'\n');print(json.dumps(evidence))
    except Exception as exc:
        (root/'failure.json').write_text(json.dumps({'status':'failed','error':repr(exc),
            'candidate':store.candidate('report.docx'),'review_status':'not_reviewed'},indent=2)+'\n')
        raise
    finally:store.close()

if __name__=='__main__':asyncio.run(main())
