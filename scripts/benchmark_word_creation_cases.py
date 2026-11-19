"""Opt-in frozen C1/C2 native diagnostics. No downloads; run only in an assigned slot.

A machine pass always remains pending human semantic/visual review. The original
page-band benchmark is unchanged. Importing this module never starts a workload.
"""
import argparse
import asyncio
import hashlib
import io
import json
import posixpath
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
SPEC=ROOT/'docs/benchmarks/word-creation-next-fixtures.md'
PIN='50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
CASES={'C1':{'steps':10,'watchdog':150,'prompt_hash':'e27e9fb8db01328dc42c675296885e381126814f057ee72322320428da184651'},
       'C2':{'steps':12,'watchdog':180,'prompt_hash':'374804a763b8ddd2ad69cdd75a62a92b77687c6759db27c43e105c49099d374b'}}
W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
P='{http://schemas.openxmlformats.org/package/2006/relationships}'
C1_LITERALS=('Síntese — café e inovação.','Conclusão — próximos passos.')


@dataclass(frozen=True)
class FixtureConfig:
    """Explicit opt-in fixture callbacks; historical C1/C2 defaults stay intact."""
    prompt: str
    prompt_hash: str
    spec: Path
    harness: Path
    steps: int
    watchdog: int
    assert_docx: Callable
    assert_pdf: Callable
    verify_candidate: Callable
    expected_generated_body_index: int | None = None
    focused_verifier_version: int = 1


def require(ok,message):
    if not ok:raise AssertionError(message)


def verify_focused_placement(store,task,body_texts,candidate_revision,config,report):
    """Fixture-only handoff check against the current active generation."""
    from localoffice.word.draft_fragments import get as get_draft
    generated=get_draft(store,task)
    require(generated is not None and generated['status']=='drafted','Focused cohort requires a submitted generated draft')
    matches=[index for index,text in enumerate(body_texts) if text==generated['text']]
    expected=config.expected_generated_body_index
    placed=bool(matches) if expected is None else expected in matches
    report['focused_draft_placement']={'ref':generated['ref'],'generation':generated['generation'],
        'instruction':generated['instruction'],'text':generated['text'],
        'expected_body_index':expected,'matching_body_indices':matches,
        'exact_body_paragraph_match':bool(matches),'expected_body_paragraph_match':placed,
        'verifier_version':config.focused_verifier_version,
        'candidate_revision':candidate_revision,'semantic_validation':'pending_reviewer'}
    require(placed,'Submitted current focused draft does not exactly match the expected body paragraph')


def sha(data):return hashlib.sha256(data).hexdigest()


def runtime_sources():
    """Identify the actual backend/worker cohort without adding model context."""
    paths=list((ROOT/'host').rglob('*.py'))+list((ROOT/'workers/openxml').rglob('*.cs'))
    paths += list((ROOT/'host/localoffice/builtin_skills').rglob('SKILL.md'))
    return {str(path.relative_to(ROOT)):sha(path.read_bytes()) for path in sorted(paths)
            if not {'bin','obj','__pycache__'}&set(path.relative_to(ROOT).parts)}


def prompt_for(case):
    spec=SPEC.read_text()
    section=spec.split('## Fixture '+case+' — ',1)[1].split('\n## ',1)[0]
    prompt=re.search(r'```text\n(.*?)\n```',section,re.S).group(1)
    require(sha(prompt.encode())==CASES[case]['prompt_hash'],'Frozen prompt changed: version the fixture before running.')
    return prompt


def enabled(node):
    if node is None:return False
    value=node.get(W+'val')
    require(value in (None,'1','true','on','0','false','off'),'Invalid on/off value')
    return value in (None,'1','true','on')


def text(p):return ''.join(t.text or '' for t in p.iter(W+'t'))


def plain_paragraph(p):
    require(all(n.tag in (W+'pPr',W+'r') for n in p),'Unexpected body paragraph structure')
    for run in p.findall(W+'r'):
        require(all(n.tag in (W+'rPr',W+'t') for n in run),'Unexpected body field, break, tab or drawing')
    require(p.find(W+'pPr/'+W+'sectPr') is None,'Unexpected paragraph section break')
    return text(p)


def assert_typography(p,styles,size):
    ref=p.find(W+'pPr/'+W+'pStyle')
    require(ref is not None and ref.get(W+'val') in styles,'Missing declared paragraph style')
    style=styles[ref.get(W+'val')]
    require(style.get(W+'type')=='paragraph','Assigned style is not a paragraph style')
    fonts=style.find(W+'rPr/'+W+'rFonts');sz=style.find(W+'rPr/'+W+'sz')
    require(fonts is not None and all(fonts.get(W+k)=='Arial' for k in ('ascii','hAnsi')),'Requested Arial typography missing')
    require(sz is not None and sz.get(W+'val')==str(size),'Incorrect named-style point size')
    for props in p.iter(W+'rPr'):
        f=props.find(W+'rFonts');s=props.find(W+'sz')
        if f is not None:
            require(not any(k.endswith('Theme') for k in f.attrib),'Unverified direct theme-font override')
            require(all(v=='Arial' for k,v in f.attrib.items() if k in (W+'ascii',W+'hAnsi')),'Conflicting direct run font')
        if s is not None:require(s.get(W+'val')==str(size),'Conflicting direct run size')


def footer_sequence(p):
    result=[]
    for child in p:
        if child.tag==W+'pPr':continue
        if child.tag==W+'r':
            require(all(n.tag in (W+'rPr',W+'t') for n in child),'Unexpected footer literal structure')
            value=text(child)
            if result and result[-1][0]=='literal':result[-1]=('literal',result[-1][1]+value)
            else:result.append(('literal',value))
        elif child.tag==W+'fldSimple':
            require(all(n.tag==W+'r' for n in child),'Unsupported field cache structure')
            for run in child:
                require(all(n.tag in (W+'rPr',W+'t') for n in run),'Unsupported field cache content')
            result.append(('field',(child.get(W+'instr') or '').strip()))
        else:raise AssertionError('Unexpected footer field/story structure')
    return result


def assert_docx(data,case):
    """Check current worker-supported XML shapes; no rendered/semantic claims."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        require(z.testzip() is None,'Corrupt package part')
        require(len(z.namelist())==len(set(z.namelist())),'Duplicate package parts')
        doc=ET.fromstring(z.read('word/document.xml'));body=doc.find(W+'body')
        require(body is not None,'Missing document body')
        require(all(n.tag in (W+'p',W+'sectPr') for n in body),'Unexpected body block')
        paragraphs=body.findall(W+'p');sections=body.findall(W+'sectPr')
        require(len(sections)==1 and list(body)[-1].tag==W+'sectPr','Expected one final section')
        values=[plain_paragraph(p) for p in paragraphs]
        if case=='C1':
            require(len(values)==3 and (values[0],values[2])==C1_LITERALS,'Incorrect C1 literal paragraphs/order')
            require(8<=len(values[1].split())<=16,'Draft word count outside 8–16; semantics still require review')
            breaks=[False,False,True];body_size=24
        else:
            require(values==[f'Body page {i}' for i in range(1,5)],'Incorrect C2 body text/order')
            breaks=[False,True,True,True];body_size=22
        for p,wanted in zip(paragraphs,breaks):
            nodes=p.findall(W+'pPr/'+W+'pageBreakBefore')
            require(len(nodes)<=1 and enabled(nodes[0] if nodes else None)==wanted,'Incorrect paragraph page break')
        style_nodes=ET.fromstring(z.read('word/styles.xml')).findall(W+'style')
        styles={s.get(W+'styleId'):s for s in style_nodes}
        require(len(styles)==len(style_nodes),'Duplicate style IDs')
        for p in paragraphs:assert_typography(p,styles,body_size)
        section=sections[0];size=section.find(W+'pgSz')
        require(size is not None and size.get(W+'w')=='11906' and size.get(W+'h')=='16838' and
                size.get(W+'orient') in (None,'portrait'),'Incorrect A4 portrait geometry')
        if case=='C1':
            margins=section.find(W+'pgMar')
            require(margins is not None and all(margins.get(W+k)=='1440' for k in ('top','right','bottom','left')),'Incorrect one-inch margins')
        relations=ET.fromstring(z.read('word/_rels/document.xml.rels'))
        links={n.get('Id'):n for n in relations}
        require(len(links)==len(relations),'Duplicate relationship IDs')
        settings=[]
        for rel in links.values():
            if rel.get('Type')==R[1:-1]+'/settings':settings.append(_part(z,rel,'settings'))
        require(len(settings)<=1,'Multiple settings relationships')
        title=section.findall(W+'titlePg')
        even=settings[0].findall(W+'evenAndOddHeaders') if settings else []
        require(len(title)<=1 and len(even)<=1,'Duplicate band activation settings')
        require(enabled(title[0] if title else None)==(case=='C2'),'Incorrect first-page activation')
        require(enabled(even[0] if even else None)==(case=='C2'),'Incorrect even-page activation')
        expected_variants={'default'} if case=='C1' else {'default','first','even'}
        parts=[];band_facts=[]
        for kind in ('header','footer'):
            references=section.findall(W+kind+'Reference')
            require(len(references)==len(expected_variants) and {r.get(W+'type') for r in references}==expected_variants,'Missing/duplicate/unexpected band references')
            for reference in references:
                variant=reference.get(W+'type');rel=links.get(reference.get(R+'id'))
                root=_part(z,rel,kind);part=_part_name(rel);parts.append(part)
                require(root.tag==W+('hdr' if kind=='header' else 'ftr'),'Wrong story root')
                require(len(root)==1 and root[0].tag==W+'p','Expected one paragraph per band')
                p=root[0];align=p.find(W+'pPr/'+W+'jc')
                require(align is not None and align.get(W+'val')==('right' if kind=='header' else 'center'),'Incorrect band alignment')
                assert_typography(p,styles,20 if case=='C1' and kind=='header' else 18)
                label={'default':'Default','first':'First','even':'Even'}[variant]
                if kind=='header':
                    expected='Relatório de revisão' if case=='C1' else label+' header'
                    require(plain_paragraph(p)==expected,'Incorrect header text')
                else:
                    prefix='Página ' if case=='C1' else label+' page '
                    separator=' de ' if case=='C1' else ' of '
                    require(footer_sequence(p)==[('literal',prefix),('field','PAGE'),('literal',separator),('field','NUMPAGES')], 'Incorrect native footer sequence')
                band_facts.append({'kind':kind,'variant':variant,'part':part})
        require(len(set(parts))==len(parts),'Band variants unexpectedly share story parts')
    return {'body_texts':values,'bands':band_facts,'xml_assertions':'passed',
            'draft_semantics':'pending_reviewer' if case=='C1' else 'not_applicable'}


def _part_name(rel):
    require(rel is not None and rel.get('TargetMode') in (None,'Internal'),'Missing or external relationship')
    target=rel.get('Target','')
    require(target and '\\' not in target and ':' not in target and '?' not in target and '#' not in target,'Invalid relationship target')
    name=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('word',target))
    require(not name.startswith('../') and name!='..','Relationship escapes package')
    return name


def _part(z,rel,kind):
    require(rel is not None and rel.get('Type')==R[1:-1]+'/'+kind,'Wrong relationship type')
    return ET.fromstring(z.read(_part_name(rel)))


def assert_pdf(data,case,body_texts):
    from pypdf import PdfReader
    reader=PdfReader(io.BytesIO(data));expected_pages=2 if case=='C1' else 4
    require(len(reader.pages)==expected_pages,'Incorrect rendered page count')
    texts=[' '.join((page.extract_text() or '').split()) for page in reader.pages]
    normalized=[' '.join(value.split()) for value in body_texts]
    for index,page in enumerate(texts):
        expected_indices=({0,1} if index==0 else {2}) if case=='C1' else {index}
        for body_index,value in enumerate(normalized):
            require(page.count(value)==(1 if body_index in expected_indices else 0),'Incorrect page-local body content')
        if case=='C1':
            require(page.count('Relatório de revisão')==1 and page.count(f'Página {index+1} de 2')==1,'Incorrect rendered C1 bands')
            require(f'Página {2-index} de 2' not in page,'Wrong page-number text leaked')
        else:
            label=['First','Even','Default','Even'][index]
            require(page.count(label+' header')==1 and page.count(f'{label} page {index+1} of 4')==1,'Incorrect rendered C2 bands')
            for other in ('First','Even','Default'):
                if other!=label:require(other+' header' not in page and other+' page ' not in page,'Band variant leaked onto wrong page')
    return {'pages':expected_pages,'page_texts':texts,'pdf_text_assertions':'passed','visual_review':'pending_reviewer'}


def export_review_images(pdf,folder):
    import pypdfium2 as pdfium
    document=pdfium.PdfDocument(pdf);paths=[]
    try:
        for i in range(len(document)):
            page=document[i];bitmap=page.render(scale=1.15)
            try:
                path=folder/f'page-{i+1}.png';bitmap.to_pil().save(path);paths.append(str(path))
            finally:bitmap.close();page.close()
    finally:document.close()
    return paths


async def run(case,output=None,*,workflow='reviewed',fixture_config=None,requirements_planning=False,focused_drafting=False,isolated_draft_planning=False):
    if workflow not in ('reviewed','direct_comparison'):
        raise ValueError('Unknown diagnostic workflow')
    reviewed=workflow=='reviewed'
    if type(requirements_planning) is not bool or (requirements_planning and not reviewed):
        raise ValueError('Requirement planning requires the reviewed diagnostic workflow')
    if fixture_config is not None and not reviewed:
        raise ValueError('Configured fixtures require their frozen reviewed workflow')
    if type(focused_drafting) is not bool or (focused_drafting and not (requirements_planning and reviewed and fixture_config is not None)):
        raise ValueError('Focused drafting requires a configured reviewed requirements fixture')
    if type(isolated_draft_planning) is not bool or (isolated_draft_planning and not focused_drafting):
        raise ValueError('Isolated draft planning requires focused drafting')
    config=fixture_config
    steps=config.steps if config else CASES[case]['steps']
    watchdog=config.watchdog if config else CASES[case]['watchdog']
    spec=config.spec if config else SPEC
    harness=config.harness if config else Path(__file__)
    source_snapshot=runtime_sources()
    if config:
        for path in (spec,harness,Path(__file__)):
            source_snapshot[str(path.relative_to(ROOT))]=sha(path.read_bytes())
    # Imports and provider construction are intentionally confined to explicit runs.
    from localoffice.storage import ProjectStore
    from localoffice.runtime import TaskRunner
    from localoffice.providers import MLXProvider
    from localoffice.metrics import summary as metrics_summary
    stamp=str(time.time_ns());folder=Path(output or ROOT/'docs/benchmarks'/f'word-creation-{case.lower()}-{stamp}').resolve()
    folder.mkdir(parents=True,exist_ok=False)
    report={'fixture':case,'fixture_version':1,'status':'running','machine_status':'not_run',
        'review':{'visual':'pending','draft_semantics':'pending' if config or case=='C1' else 'not_applicable','reviewer':None},
        'limits':{'context':4096,'output_reserve':768,'safety_reserve':128,'max_steps':steps,'watchdog_seconds':watchdog},
        'efficiency_scope':'Observed diagnostic measurements; not release-efficiency qualification.',
        'native_constrained':True,'source_planning':reviewed,'creation_review':reviewed,
        'requirements_planning':requirements_planning,'focused_drafting':focused_drafting,'isolated_draft_planning':isolated_draft_planning,
        'workflow':workflow,'cohort':(f'isolated_draft_planning_reviewed_v{config.focused_verifier_version}' if isolated_draft_planning else f'focused_drafting_reviewed_v{config.focused_verifier_version}' if focused_drafting else 'requirements_reviewed_v1' if requirements_planning else
            'frozen_reviewed_v1' if reviewed else 'direct_architecture_comparison_v1'),
        'runtime_source_hashes':source_snapshot,
        'runtime_fingerprint':sha(json.dumps(source_snapshot,sort_keys=True).encode())}
    if focused_drafting:report['focused_verifier_version']=config.focused_verifier_version
    store=provider=runner=None;task=None;started=time.monotonic()
    try:
        prompt=config.prompt if config else prompt_for(case)
        if config:require(sha(prompt.encode())==config.prompt_hash,'Frozen configured prompt changed')
        report.update(prompt=prompt,prompt_hash=sha(prompt.encode()),spec_hash=sha(spec.read_bytes()),harness_hash=sha(harness.read_bytes()))
        approval=json.loads((ROOT/'docs/benchmarks/approved-mlx-download.json').read_text());model=approval['model']
        require(approval['revision']==PIN and Path(model['path']).name==PIN and Path(model['path']).is_dir(),'Pinned local model unavailable; no download or substitution allowed')
        report['model']=model
        project=Path(tempfile.mkdtemp(prefix=f'localoffice-word-{case.lower()}-'));report['project']=str(project)
        store=ProjectStore(project)
        if config:
            require(not (project/'report.docx').exists() and store.candidate('report.docx') is None,'B1 target must start absent')
            report['target_initially_absent']=True
        provider=MLXProvider(structural_decoding=False,native_tool_protocol=True,native_constrained=True,sampling_profile='low_temperature')
        report['sampling']=provider.sampling
        if config:require(provider.sampling=={'temperature':0.2,'top_p':1.0,'top_k':0,'min_p':0.0},'Frozen sampling profile changed')
        runner=TaskRunner(store,provider,source_planning=reviewed,creation_review=reviewed,
                          requirements_planning=requirements_planning,focused_drafting=focused_drafting,isolated_draft_planning=isolated_draft_planning);runner.max_steps=steps
        await provider.start(model['path'],4096)
        task=await runner.start(prompt,artifact='report.docx',mode='live');report['task_id']=task
        await asyncio.wait_for(runner.job,watchdog)
        report['task_status']=store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]
        require(report['task_status']=='completed','Task did not complete: '+report['task_status'])
        require(not any(e['type'] in ('error','question') for e in runner.events),'Terminal error or unnecessary clarification')
        candidate=store.candidate('report.docx');require(candidate is not None,'No staged candidate')
        require(not (project/'report.docx').exists(),'Target saved before user-side application')
        require(store.db.execute('SELECT count(*) FROM operations WHERE task_id=?',(task,)).fetchone()[0]==1,'Expected exactly one document-staging operation')
        docx=store.blob(candidate['candidate_hash']);report['artifact_assertions']=config.assert_docx(docx) if config else assert_docx(docx,case)
        if focused_drafting:
            verify_focused_placement(store,task,report['artifact_assertions']['body_texts'],
                                     candidate['candidate_hash'],config,report)
        previews=[e for e in runner.events if e['type']=='candidate' and e.get('render') and e['revision']==candidate['candidate_hash']]
        require(len(previews)==1,'Expected one revision-bound live candidate preview')
        render=previews[0]['render'];require(render['path']=='report.docx' and render['revision']==candidate['candidate_hash'],'Render provenance differs from candidate')
        pdf=store.blob(render['pdf_hash']);report['pdf_assertions']=(config.assert_pdf(pdf,report['artifact_assertions']['body_texts'])
            if config else assert_pdf(pdf,case,report['artifact_assertions']['body_texts']))
        if config:report['post_task_artifact_checks']=config.verify_candidate(store,task,dict(candidate),render)
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        saved=(project/'report.docx').read_bytes();require(saved==docx,'Save changed candidate bytes')
        if config:require(store.candidate('report.docx') is None,'Save left a document working copy')
        undo=store.undo(candidate['operation_id'],'fixture-undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        require(not (project/'report.docx').exists(),'Undo did not restore target absence')
        if config:require(store.candidate('report.docx') is None,'Applied Undo left a document working copy')
        restore=store.undo(undo['id'],'fixture-restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        require((project/'report.docx').read_bytes()==saved,'Restore changed saved bytes')
        if config:require(store.candidate('report.docx') is None,'Applied restore left a document working copy')
        report.update(machine_status='passed',status='machine_pass_review_pending',save_undo_restore={'exact':True,'saved_hash':sha(saved),'undo_id':undo['id'],'restore_id':restore['id']})
    except asyncio.CancelledError:
        report.update(status='interrupted',machine_status='incomplete',error='CancelledError')
        raise
    except Exception as exc:report.update(status='failed',machine_status='failed',error=repr(exc))
    finally:
        # Persist evidence even after a failed task/assertion. Never rerender to
        # manufacture a missing live boundary or repair a failed candidate.
        if runner is not None:
            try:await runner.pause()
            except Exception as exc:report['pause_error']=repr(exc)
            report['events']=runner.events
        if store is not None and task is not None:
            try:
                if focused_drafting:
                    from localoffice.word.draft_fragments import get as get_draft
                    generated=get_draft(store,task)
                    report['focused_draft_state']=({'ref':generated['ref'],'generation':generated['generation'],
                        'status':generated['status'],'instruction':generated['instruction'],
                        'text':generated.get('text'),'semantic_validation':'unassessed'} if generated else None)
                report['metrics']=metrics_summary(store,task)
                report.setdefault('task_status',store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0])
                skill=store.db.execute('SELECT name,source_hash FROM task_skills WHERE task_id=?',(task,)).fetchone()
                report['selected_skill']=dict(skill) if skill else None
                candidate=store.candidate('report.docx')
                if candidate is None:
                    candidate=store.db.execute("SELECT candidate_hash,id AS operation_id,path FROM operations WHERE task_id=? AND tool='word.stage' ORDER BY created DESC LIMIT 1",(task,)).fetchone()
                if candidate:
                    report['candidate']=dict(candidate);revision=candidate['candidate_hash']
                    data=store.blob(revision);(folder/'candidate.docx').write_bytes(data)
                    report['candidate_file']=str(folder/'candidate.docx')
                    previews=[e for e in runner.events if e['type']=='candidate' and e.get('render') and e.get('revision')==revision]
                    if previews:
                        render=previews[-1]['render'];pdf=store.blob(render['pdf_hash']);(folder/'preview.pdf').write_bytes(pdf)
                        report['render']=render;report['pdf_file']=str(folder/'preview.pdf')
                        report['review']['page_images']=export_review_images(pdf,folder)
                        report['review'].update(revision=revision,pdf_hash=render['pdf_hash'],viewed_pages=[])
            except Exception as exc:report['evidence_error']=repr(exc);report['status']='failed'
        if provider is not None:
            try:await provider.close()
            except Exception as exc:report['provider_close_error']=repr(exc)
        if store is not None:store.close()
        final_sources=runtime_sources()
        if config:
            for path in (spec,harness,Path(__file__)):
                final_sources[str(path.relative_to(ROOT))]=sha(path.read_bytes())
        report['runtime_sources_unchanged']=final_sources==source_snapshot
        if not report['runtime_sources_unchanged']:
            report.update(status='failed',machine_status='failed',
                          source_change_error='Runtime sources changed during this attempt; it cannot qualify a stable cohort.')
        report['elapsed_seconds']=time.monotonic()-started
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'report':str(folder/'report.json'),'status':report['status'],'machine_status':report['machine_status'],'review':report['review']},ensure_ascii=False))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=tuple(CASES),required=True)
    parser.add_argument('--output',type=Path,help='New evidence directory; must not already exist.')
    parser.add_argument('--workflow',choices=('reviewed','direct_comparison'),default='reviewed',
        help='Direct comparison is a separate diagnostic cohort, never a frozen reviewed-case pass.')
    args=parser.parse_args()
    outcome=asyncio.run(run(args.case,args.output,workflow=args.workflow))
    # Zero means machine checks succeeded; report explicitly still awaits review.
    raise SystemExit(0 if outcome['status']=='machine_pass_review_pending' else 1)
