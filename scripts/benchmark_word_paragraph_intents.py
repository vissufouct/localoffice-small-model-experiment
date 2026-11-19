"""Fixed semantic contrasts for the pinned native Qwen profile; retain every case."""
import asyncio,hashlib,io,json,sys,tempfile,time,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_word_creation import design,W
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.runtime import TaskRunner
from localoffice.providers import MLXProvider
from localoffice.metrics import summary

CASES=[
    ('remove_intro','delete','Evidence-backed introduction.','In report.docx remove the entire paragraph whose exact text is Evidence-backed introduction. Preserve every other paragraph, all formatting, lists, tables and the explicit page break. Stage the working copy and finish.'),
    ('remove_list_item','delete','First item','In report.docx remove the entire paragraph whose exact text is First item. Preserve every other paragraph, all formatting, lists, tables and the explicit page break. Stage the working copy and finish.'),
    ('clear_keep_paragraph','clear','Evidence-backed introduction.','In report.docx clear all the text from the paragraph whose exact text is Evidence-backed introduction. Keep that empty paragraph, its style and spacing, and all other content and formatting. Stage the working copy and finish.'),
    ('read_do_not_delete','read','Evidence-backed introduction.','Do not remove any paragraph from report.docx. Read the paragraph whose exact text is Evidence-backed introduction. and report its text. Do not edit the file or stage any changes.'),
]


def verify_xml(original,candidate,target,operation):
    with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(candidate)) as new:
        assert set(old.namelist())==set(new.namelist())
        for name in old.namelist():
            if name!='word/document.xml':assert old.read(name)==new.read(name),name
        a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
        body=a.find('{'+W+'}body')
        matches=[p for p in body if p.tag=='{'+W+'}p' and ''.join(p.itertext())==target]
        assert len(matches)==1
        if operation=='delete':body.remove(matches[0])
        elif operation=='clear':
            for node in matches[0].iter('{'+W+'}t'):
                node.text='';node.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
        else:raise AssertionError('Unknown expected operation')
        assert ET.canonicalize(ET.tostring(a))==ET.canonicalize(ET.tostring(b)), 'Candidate differs from requested operation or preservation requirements'


async def run_case(provider,case):
    name,operation,target,prompt=case
    root=Path(tempfile.mkdtemp(prefix='word-intent-'+name+'-'))
    store=ProjectStore(root);plugin=WordPlugin(store);runner=TaskRunner(store,provider)
    fixture=plugin.create(**design(),idempotency_key='fixture')
    store.apply(fixture['receipt_id'],expected_candidate=fixture['candidate_revision'],action='save')
    original=(root/'report.docx').read_bytes();started=time.monotonic()
    report={'case':name,'operation':operation,'target':target,'prompt':prompt,'project':str(root),'input_revision':fixture['candidate_revision'],'status':'started'}
    try:
        key=await runner.start(prompt,artifact='report.docx');await asyncio.wait_for(runner.job,150)
        report['metrics']=summary(store,key);report['events']=runner.events
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed','Task did not complete'
        count=store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(key,)).fetchone()[0]
        assert (root/'report.docx').read_bytes()==original,'Original changed before Apply'
        candidate=store.candidate('report.docx')
        if operation=='read':
            assert candidate is None and count==0,'Read-only task staged an edit'
            last=json.loads(store.db.execute("SELECT content FROM messages WHERE task_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(key,)).fetchone()[0])
            assert last['tool']=='finish' and target in last['text'],'Requested source text missing from response'
            report['read_only_verified']=True
        else:
            assert candidate and count==1,'Expected exactly one requested mutation'
            verify_xml(original,store.blob(candidate['candidate_hash']),target,operation)
            previews=[e for e in runner.events if e['type']=='candidate' and e.get('render')]
            assert len(previews)==1 and previews[0]['revision']==candidate['candidate_hash']
            report.update(candidate_revision=candidate['candidate_hash'],render=previews[0]['render'],visual_review='pending')
            store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='apply')
            undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
            assert (root/'report.docx').read_bytes()==original
            report['exact_undo']=True
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:
        await runner.pause();store.close();report['elapsed_seconds']=time.monotonic()-started
    return report


async def main():
    model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['model']
    provider=MLXProvider(native_tool_protocol=True,native_constrained=True)
    sources=[*Path('host/localoffice').rglob('*.py'),*Path('host/localoffice/builtin_skills').rglob('*.md'),*Path('workers/openxml').glob('*.cs'),Path('workers/openxml/bin/Debug/net10.0/LocalOffice.OpenXml.dll'),Path(__file__),Path('tests/test_word_creation.py')]
    path=Path('docs/benchmarks/word-paragraph-intents-'+str(time.time_ns())+'.json')
    report={'status':'planned','planned_cases':[list(c) for c in CASES],'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'cases':[],'timing':'Per-case time excludes initial model loading; one owned worker shared sequentially, fresh project/chat for every case.','acceptance':'All four fixed cases must pass complete XML/package preservation or no-edit checks, exact mutation count, rendered candidate identity and exact Undo. No prompt or assertion changes during trials. Visual review separate; not release qualification.'}
    def save():path.write_text(json.dumps(report,indent=2)+'\n')
    save()
    try:
        await provider.start(model['path'],4096);report['profile']=provider.profile;report['status']='running';save()
        for case in CASES:
            for p,h in report['source_sha256'].items():assert hashlib.sha256(Path(p).read_bytes()).hexdigest()==h,p
            result=await run_case(provider,case);report['cases'].append(result);save()
            print(json.dumps({k:v for k,v in result.items() if k not in ('metrics','events','render')}),flush=True)
        report['status']='passed' if all(c['status']=='passed' for c in report['cases']) else 'failed'
    except Exception as exc:report.update(status='failed',error=repr(exc))
    finally:await provider.close();save();print('Report:',path,flush=True)
    return report['status']=='passed'

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
