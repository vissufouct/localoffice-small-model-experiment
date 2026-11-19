"""Frozen B1 body-only diagnostic; imports never load models or run workers.

Machine checks are not semantic/visual approval. Run only in an assigned model
slot; historical C1/C2 remain separate, unchanged diagnostic fixtures.
"""
import argparse
import asyncio
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from benchmark_word_creation_cases import (
    ROOT, W, R, P, FixtureConfig, enabled, plain_paragraph, require, sha,
    _part, run as run_fixture,
)

SPEC=ROOT/'docs/benchmarks/word-body-iteration-fixtures.md'
PROMPT_HASH='6f59c8aac0d8afc3a2ec741e14233add87b4be2957eb7b4fa4d4bf36a2ab4395'
LITERALS=('Síntese — café e inovação.','Conclusão — próximos passos.')


def prompt_for():
    matches=re.findall(r'```text\n(.*?)\n```',SPEC.read_text(),re.S)
    require(len(matches)==1 and sha(matches[0].encode())==PROMPT_HASH,'Frozen B1 prompt changed; version the fixture first')
    return matches[0]


def _one(parent,tag):
    nodes=parent.findall(W+tag) if parent is not None else []
    require(len(nodes)<=1,'Duplicate '+tag)
    return nodes[0] if nodes else None


def _chain(styles,identity,kind,seen=()):
    require(identity in styles and identity not in seen,'Missing or cyclic style inheritance')
    style=styles[identity]
    require(style.get(W+'type')==kind,'Wrong style type')
    parent=_one(style,'basedOn')
    return (_chain(styles,parent.get(W+'val'),kind,(*seen,identity)) if parent is not None else [])+[style]


def _run_format(properties,styles,seen=()):
    fonts={};sizes={}
    for props in properties:
        if props is None:continue
        character=_one(props,'rStyle')
        if character is not None:
            identity=character.get(W+'val')
            require(identity not in seen,'Cyclic character-style reference')
            f,s=_run_format([_one(st,'rPr') for st in _chain(styles,identity,'character')],styles,(*seen,identity))
            fonts.update(f);sizes.update(s)
        font=_one(props,'rFonts')
        if font is not None:fonts.update(font.attrib)
        for name in ('sz','szCs'):
            node=_one(props,name)
            if node is not None:sizes[name]=node.get(W+'val')
        for name in ('vanish','webHidden'):
            require(not enabled(_one(props,name)),'Hidden body text is not permitted')
    return fonts,sizes


def _check_font(properties,styles):
    fonts,sizes=_run_format(properties,styles)
    require(not any(key.endswith('Theme') for key in fonts),'Theme-font override is not verified Arial')
    require(all(fonts.get(W+name)=='Arial' for name in ('ascii','hAnsi')),'Body font is not Arial')
    require(all(fonts.get(W+name,'Arial')=='Arial' for name in ('eastAsia','cs')),'Conflicting script font')
    require(sizes.get('sz')=='24' and sizes.get('szCs','24')=='24','Body size is not 12 point')


def _page_break(layers):
    state=False
    for props in layers:
        node=_one(props,'pageBreakBefore')
        if node is not None:state=enabled(node)
    return state


def assert_docx(data):
    """Structural/style assertions only; English invitation semantics stay pending."""
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        require(package.testzip() is None,'Corrupt ZIP part')
        require(len(package.namelist())==len(set(package.namelist())),'Duplicate package parts')
        roots={name:ET.fromstring(package.read(name)) for name in package.namelist()
               if name.endswith(('.xml','.rels'))}
        forbidden_roots={W+name for name in ('hdr','ftr','footnotes','endnotes','comments')}
        for name,root in roots.items():
            require(root.tag not in forbidden_roots,'Unrequested story part, including orphan parts')
            require(not any(node.tag in {W+'fldSimple',W+'fldChar',W+'instrText',W+'drawing',W+'pict',W+'tbl',W+'sdt'}
                            for node in root.iter()),'Unrequested field, image, table or content control')
            if name.endswith('.rels'):
                ids=[node.get('Id') for node in root]
                require(len(ids)==len(set(ids)),'Duplicate relationship IDs')
                require(not any((node.get('Type') or '').rsplit('/',1)[-1] in
                                ('header','footer','image','footnotes','endnotes','comments') for node in root),
                        'Unrequested story/image relationship')
        require(not any(name.startswith(('word/media/','word/embeddings/')) for name in package.namelist()),'Unrequested media part')
        document=roots['word/document.xml'];body=_one(document,'body')
        require(body is not None,'Missing body')
        require([node.tag for node in body]==[W+'p',W+'p',W+'p',W+'sectPr'],'Expected exactly three paragraphs and one final section')
        paragraphs=body.findall(W+'p');values=[plain_paragraph(p) for p in paragraphs]
        require((values[0],values[2])==LITERALS,'Exact body literals/order changed')
        require(8<=len(values[1].split())<=16,'Draft must contain 8–16 whitespace-separated words')
        require(not any(char in values[1] for char in '\r\n'),'Draft must be one plain paragraph')
        # Sentence meaning/language are deliberately not inferred from a regex.
        section=body[-1];size=_one(section,'pgSz');margins=_one(section,'pgMar')
        require(size is not None and size.get(W+'w')=='11906' and size.get(W+'h')=='16838'
                and size.get(W+'orient') in (None,'portrait'),'Incorrect A4 portrait size')
        require(margins is not None and all(margins.get(W+n)=='1440' for n in ('top','right','bottom','left')),'Incorrect one-inch margins')
        require(margins.get(W+'gutter','0')=='0','Unrequested gutter changes usable margins')
        require(not section.findall(W+'headerReference') and not section.findall(W+'footerReference'),'Unexpected band references')
        require(not enabled(_one(section,'titlePg')),'Unexpected active first-page setting')
        require(not section.findall(W+'pgNumType'),'Unexpected page-number setting')
        links=roots['word/_rels/document.xml.rels']
        style_links=[node for node in links if node.get('Type')==R[1:-1]+'/styles']
        require(len(style_links)==1,'Expected one styles relationship')
        styles_root=_part(package,style_links[0],'styles')
        nodes=styles_root.findall(W+'style');styles={node.get(W+'styleId'):node for node in nodes}
        require(None not in styles and len(styles)==len(nodes),'Missing/duplicate style IDs')
        settings=[_part(package,node,'settings') for node in links if node.get('Type')==R[1:-1]+'/settings']
        require(len(settings)<=1,'Duplicate settings relationships')
        if settings:require(not enabled(_one(settings[0],'evenAndOddHeaders')),'Unexpected active even-page setting')
        defaults=_one(styles_root,'docDefaults')
        default_runs=_one(_one(defaults,'rPrDefault'),'rPr')
        default_paragraph=_one(_one(defaults,'pPrDefault'),'pPr')
        for index,paragraph in enumerate(paragraphs):
            props=_one(paragraph,'pPr');reference=_one(props,'pStyle')
            require(reference is not None,'Every paragraph needs an explicit named style')
            chain=_chain(styles,reference.get(W+'val'),'paragraph')
            run_layers=[default_runs,*(_one(st,'rPr') for st in chain)]
            _check_font(run_layers,styles)
            paragraph_layers=[default_paragraph,*(_one(st,'pPr') for st in chain),props]
            require(not any(_one(layer,'numPr') is not None for layer in paragraph_layers),'Unrequested list numbering')
            require(_page_break(paragraph_layers)==(index==2),'Incorrect effective paragraph page break')
            if index==2:require(enabled(_one(props,'pageBreakBefore')),'Third paragraph requires direct pageBreakBefore')
            _check_font([*run_layers,_one(props,'rPr')],styles)
            for run in paragraph.findall(W+'r'):_check_font([*run_layers,_one(run,'rPr')],styles)
    return {'body_texts':values,'xml_assertions':'passed','draft_semantics':'pending_reviewer',
            'semantic_review_required':'One neutral English invitation sentence, without completed-review or organisational claims.'}


def assert_pdf(data,body_texts):
    from pypdf import PdfReader
    reader=PdfReader(io.BytesIO(data))
    require(len(reader.pages)==2,'Expected exactly two rendered pages')
    texts=[' '.join((page.extract_text() or '').split()) for page in reader.pages]
    expected=[' '.join(' '.join(body_texts[:2]).split()),' '.join(body_texts[2].split())]
    require(texts==expected,'Rendered pages contain missing, duplicate, extra or misplaced text')
    return {'pages':2,'page_texts':texts,'pdf_text_assertions':'passed','visual_review':'pending_reviewer'}


def verify_candidate(store,task_id,candidate,render):
    """Post-task harness checks, never model-issued corrections or approvals."""
    from localoffice.word.plugin import WordPlugin
    task=store.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    require(task and task['artifact']=='report.docx' and task['status']=='completed','Wrong task target/status')
    operation=dict(store.operation(candidate['operation_id']))
    require(operation['task_id']==task_id and operation['tool']=='word.stage' and operation['status']=='validated','Wrong candidate operation')
    require(operation['path']==candidate['path']==render['path']=='report.docx','Candidate/render path mismatch')
    require(operation['candidate_hash']==candidate['candidate_hash']==render['revision'],'Candidate/render revision mismatch')
    require(operation['input_hash'] is None and candidate['base_hash'] is None,'Creation overwrote an existing base')
    require(not (store.root/'report.docx').exists(),'Target saved before harness application')
    require(store.db.execute('SELECT count(*) FROM candidates').fetchone()[0]==1,'Expected one staged working copy')
    require(sha(store.blob(candidate['candidate_hash']))==candidate['candidate_hash'],'Candidate hash mismatch')
    require(sha(store.blob(render['pdf_hash']))==render['pdf_hash'] and render['pages']==2,'PDF hash/count mismatch')
    validation=store.db.execute("SELECT detail FROM events WHERE operation_id=? AND state='validated' ORDER BY seq LIMIT 1",(operation['id'],)).fetchone()
    require(validation is not None and json.loads(validation[0]).get('openxml') is True,'Missing worker OOXML validation')
    require(sha(operation['args'].encode())==operation['args_hash'],'Operation provenance hash mismatch')
    exported=WordPlugin(store).export_pdf(path='report.docx',revision=candidate['candidate_hash'],
        pdf_hash=render['pdf_hash'],destination='report.pdf',idempotency_key='b1-harness-pdf-export')
    require(exported['candidate_hash']==render['pdf_hash'],'PDF export changed rendered bytes')
    store.apply(exported['id'],expected_candidate=render['pdf_hash'],action='save')
    require(sha((store.root/'report.pdf').read_bytes())==render['pdf_hash'],'Saved PDF differs from exact preview')
    require(store.candidate('report.pdf') is None,'Saved PDF left an export candidate')
    return {'worker_openxml_validation':True,'pdf_export':{'operation_id':exported['id'],
        'source_revision':candidate['candidate_hash'],'pdf_hash':render['pdf_hash'],'path':'report.pdf'},
        'scope':'Post-task harness verification; no visual or semantic approval.'}


def configuration():
    return FixtureConfig(prompt=prompt_for(),prompt_hash=PROMPT_HASH,spec=SPEC,harness=Path(__file__),
                         steps=10,watchdog=150,assert_docx=assert_docx,assert_pdf=assert_pdf,verify_candidate=verify_candidate,
                         expected_generated_body_index=1,focused_verifier_version=2)


async def run(output=None,*,requirements_planning=False,focused_drafting=False,isolated_draft_planning=False):
    return await run_fixture('B1',output,fixture_config=configuration(),requirements_planning=requirements_planning,focused_drafting=focused_drafting,isolated_draft_planning=isolated_draft_planning)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,help='New B1 evidence directory; must not exist.')
    parser.add_argument('--requirements-planning',action='store_true',
                        help='Explicit experimental requirement-planning cohort; prompt and acceptance limits unchanged.')
    parser.add_argument('--focused-drafting',action='store_true',help='Separate focused-drafting cohort; requires --requirements-planning.')
    parser.add_argument('--isolated-draft-planning',action='store_true',help='Separate isolated-planning cohort; requires focused drafting and shares its task limits.')
    arguments=parser.parse_args()
    outcome=asyncio.run(run(arguments.output,requirements_planning=arguments.requirements_planning,focused_drafting=arguments.focused_drafting,isolated_draft_planning=arguments.isolated_draft_planning))
    raise SystemExit(0 if outcome['status']=='machine_pass_review_pending' else 1)
