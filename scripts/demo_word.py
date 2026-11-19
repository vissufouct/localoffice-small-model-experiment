"""Real local MLX compact Word edit; isolated synthetic fixture, no downloads."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_worker import fixture_docx
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary
from localoffice.discovery import hardware

async def main(annotated=False,insert=False,formatting=False,native=False):
    root=Path(tempfile.mkdtemp(prefix='localoffice-word-demo-'))
    target=root/'brief.docx';fixture_docx(target);original=target.read_bytes()
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    store=ProjectStore(root);provider=MLXProvider(native_tool_protocol=native);runner=TaskRunner(store,provider)
    report={'status':'started','native_tool_protocol':native,'hardware':hardware(),'model':model,'context':4096,'project':str(root)}
    started=time.monotonic()
    try:
        await provider.start(model['path'],4096)
        skill=runner.skills.create_draft('word-compact-edit','Make a bounded Word edit.',
            'Find only the relevant paragraph. Use exact returned revision and object hash. Stage a compact replacement; do not reproduce unchanged content. Finish after a successful receipt.',
            ['word.list_objects','word.read_objects','word.stage_batch'])
        annotation_ids=[]
        if annotated:
            from localoffice.annotations import annotate
            revision=store.file_hash('brief.docx')
            obj=runner.registry.word.list_objects(path='brief.docx',revision=revision,limit=1)['objects'][0]
            annotation_ids=[annotate(store,path='brief.docx',revision=revision,scope='selection',locator={'object_id':obj['id'],'object_hash':obj['hash']},comment='Replace initial A with Z; only this paragraph may change.')]
        prompt=('In brief.docx insert exactly two new paragraphs immediately after the first paragraph: "First addition" and "Second addition". Use the existing Heading1 style for both. Preserve both original paragraphs and all their formatting. Read the anchor, stage one insert_paragraphs batch, then finish.' if insert else 'In brief.docx replace only the first character A of the first paragraph with Z. Read the first paragraph using Word tools, stage the compact edit, then finish. Preserve every other character and all formatting.')
        if formatting:prompt='In brief.docx center the first paragraph and make all its text 16 points. Preserve all text and every other property and paragraph. Read the target, stage a compact formatting operation, then finish.'
        task=await runner.start(prompt,artifact='brief.docx',skill_id=None if insert or formatting else skill['id'],annotation_ids=annotation_ids)
        await asyncio.wait_for(runner.job,150)
        pinned=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone()
        report['selected_skill']=dict(pinned) if pinned else None
        report['metrics']=summary(store,task)
        state=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        if state!='completed':raise RuntimeError('Task '+state+': '+json.dumps(runner.events[-4:]))
        candidate=store.candidate('brief.docx')
        if not candidate:raise RuntimeError('No staged Word candidate')
        if target.read_bytes()!=original:raise RuntimeError('Original changed before Apply')
        result=runner.registry.word.read_objects(path='brief.docx',revision=candidate['candidate_hash'],ids=['paragraph:1','paragraph:2','paragraph:3','paragraph:4'] if insert else ['paragraph:1','paragraph:2'],include_format=formatting)
        expected=['A😀BCDEFG','First addition','Second addition','Unchanged paragraph'] if insert else ['A😀BCDEFG','Unchanged paragraph'] if formatting else ['Z😀BCDEFG','Unchanged paragraph']
        if [o['text'] for o in result['objects']]!=expected:
            raise RuntimeError('Unexpected edited text: '+json.dumps(result))
        if formatting:
            direct=result['objects'][0]['direct_format']
            assert direct['properties']['alignment']=='center'
            assert all(run['size_half_points']=='32' for run in direct['runs'])
        count=store.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        if count!=1:raise RuntimeError('Expected exactly one staged batch, got '+str(count))
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
        undo=store.undo(candidate['operation_id'],'demo-undo')
        store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        if target.read_bytes()!=original:raise RuntimeError('Undo did not restore exact package bytes')
        report.update(status='passed',original_unchanged_until_apply=True,exact_undo=True,one_compact_batch=True,selected_paragraph_scope=annotated)
    except Exception as exc:report.update(status='failed',error=str(exc))
    finally:
        await runner.pause();await provider.close();store.close()
        report['elapsed_seconds']=time.monotonic()-started
        Path('docs/benchmarks/word-native-'+str(time.time_ns())+'.json' if native else 'docs/benchmarks/word-format-mlx.json' if formatting else 'docs/benchmarks/word-insert-mlx.json' if insert else 'docs/benchmarks/word-annotated-mlx.json' if annotated else 'docs/benchmarks/word-compact-mlx.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('hardware','model','metrics')},indent=2))
    return report['status']=='passed'

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--annotated',action='store_true');parser.add_argument('--insert',action='store_true');parser.add_argument('--formatting',action='store_true');parser.add_argument('--native',action='store_true');args=parser.parse_args()
    if sum([args.annotated,args.insert,args.formatting])>1:parser.error('Choose one isolated fixture mode.')
    raise SystemExit(0 if asyncio.run(main(args.annotated,args.insert,args.formatting,args.native)) else 1)
