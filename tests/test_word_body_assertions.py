"""Synthetic B1 artifact guards; no model, SDK worker, or rendering runs."""
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from localoffice.storage import ProjectStore,canonical,digest
from test_creation_evidence import xml,rels,zipped
from test_word_progress import task

ROOT=Path(__file__).resolve().parents[1]
# Load the shared lifecycle under its normal sibling-module name without
# executing either CLI. No provider construction occurs during these imports.
if 'benchmark_word_creation_cases' not in sys.modules:
    spec=importlib.util.spec_from_file_location('benchmark_word_creation_cases',ROOT/'scripts/benchmark_word_creation_cases.py')
    shared=importlib.util.module_from_spec(spec);sys.modules[spec.name]=shared;spec.loader.exec_module(shared)
spec=importlib.util.spec_from_file_location('benchmark_word_body',ROOT/'scripts/benchmark_word_body.py')
body=importlib.util.module_from_spec(spec);spec.loader.exec_module(body)
DRAFT='Please review this document and share any suggestions for improvement.'


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled',[False,True])
async def test_requirement_comparison_keeps_frozen_fixture(monkeypatch,tmp_path,enabled):
    calls=[]
    async def capture(case,output,**kwargs):
        calls.append((case,output,kwargs));return {'status':'captured_without_inference'}
    monkeypatch.setattr(body,'run_fixture',capture)
    assert await body.run(tmp_path,requirements_planning=enabled)=={'status':'captured_without_inference'}
    case,output,kwargs=calls[0]
    assert case=='B1' and output==tmp_path and kwargs['requirements_planning'] is enabled
    config=kwargs['fixture_config']
    assert config.prompt_hash==body.PROMPT_HASH and config.prompt==body.prompt_for()
    assert config.steps==10 and config.watchdog==150
    assert config.assert_docx is body.assert_docx and config.assert_pdf is body.assert_pdf


def paragraph(value,before=False):
    return ('<w:p><w:pPr><w:pStyle w:val="Body"/>'+('<w:pageBreakBefore/>' if before else '')+
            '</w:pPr><w:r><w:t xml:space="preserve">'+escape(value)+'</w:t></w:r></w:p>')


def entries():
    return {'_rels/.rels':rels([('main','officeDocument','word/document.xml','')]),
        'word/document.xml':xml('document','<w:body>'+paragraph(body.LITERALS[0])+paragraph(DRAFT)+paragraph(body.LITERALS[1],True)+
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838" w:orient="portrait"/>'+
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body>'),
        'word/_rels/document.xml.rels':rels([('styles','styles','styles.xml',''),('settings','settings','settings.xml','')]),
        'word/styles.xml':xml('styles','<w:style w:type="paragraph" w:styleId="Body"><w:name w:val="Body"/>'+
            '<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="24"/></w:rPr></w:style>'),
        'word/settings.xml':xml('settings','')}


def changed(part,old,new):
    data=entries();assert old in data[part];data[part]=data[part].replace(old,new,1)
    return zipped(data)


def pdf(pages):
    from reportlab.pdfgen.canvas import Canvas
    target=io.BytesIO();canvas=Canvas(target,pagesize=(595,842))
    for lines in pages:
        for index,line in enumerate(lines):canvas.drawString(72,760-index*25,line)
        canvas.showPage()
    canvas.save();return target.getvalue()


def test_frozen_b1_prompt_and_configuration_are_separate_from_c1_c2():
    config=body.configuration()
    assert digest(config.prompt.encode())==body.PROMPT_HASH
    assert config.steps==10 and config.watchdog==150
    assert config.spec.name=='word-body-iteration-fixtures.md'
    assert config.assert_docx is body.assert_docx
    import benchmark_word_creation_cases as shared
    assert shared.CASES['C1']['steps']==10 and shared.CASES['C2']['steps']==12
    assert shared.prompt_for('C1')!=config.prompt and shared.prompt_for('C2')!=config.prompt
    assert shared.CASES['C1']['prompt_hash']=='e27e9fb8db01328dc42c675296885e381126814f057ee72322320428da184651'


def test_supported_body_fixture_requires_semantic_and_visual_review():
    facts=body.assert_docx(zipped(entries()))
    assert facts['body_texts']==[body.LITERALS[0],DRAFT,body.LITERALS[1]]
    assert facts['draft_semantics']=='pending_reviewer'
    rendered=body.assert_pdf(pdf([facts['body_texts'][:2],facts['body_texts'][2:]]),facts['body_texts'])
    assert rendered['pages']==2 and rendered['visual_review']=='pending_reviewer'


@pytest.mark.parametrize('old,new',[
    (body.LITERALS[0],body.LITERALS[0]+' '),(body.LITERALS[1],body.LITERALS[0]),
    (DRAFT,'Too short.'),('<w:sectPr>','<w:p/><w:sectPr>'),
    ('<w:sectPr>','<w:tbl/><w:sectPr>'),('<w:sectPr>','<w:sdt/><w:sectPr>'),
    ('<w:sectPr>','<w:sectPr/><w:sectPr>'),
    ('<w:pPr>','<w:pPr><w:sectPr/>'),
    ('<w:r><w:t','<w:r><w:br w:type="page"/><w:t'),
    ('<w:r><w:t','<w:r><w:drawing/><w:t'),
    ('<w:r><w:t','<w:r><w:fldChar w:fldCharType="begin"/><w:t'),
    ('<w:r><w:t','<w:r><w:instrText>PAGE</w:instrText><w:t'),
    ('<w:r><w:t','<w:fldSimple w:instr="PAGE"/><w:r><w:t'),
    ('<w:pageBreakBefore/>','<w:pageBreakBefore w:val="off"/>'),
    ('<w:pageBreakBefore/>','<w:pageBreakBefore/><w:pageBreakBefore/>'),
    ('<w:pPr>','<w:pPr><w:pageBreakBefore/>'),
    ('w:w="11906"','w:w="12240"'),('w:orient="portrait"','w:orient="landscape"'),
    ('w:left="1440"','w:left="720"'),('w:left="1440"','w:left="1440" w:gutter="120"'),
    ('<w:sectPr>','<w:sectPr><w:titlePg/>'),
    ('<w:sectPr>','<w:sectPr><w:footerReference w:type="default" r:id="unknown"/>'),
    ('<w:sectPr>','<w:sectPr><w:pgNumType w:start="1"/>'),
])
def test_body_structure_and_pagination_false_positive_guards(old,new):
    with pytest.raises(AssertionError):body.assert_docx(changed('word/document.xml',old,new))


@pytest.mark.parametrize('font',[
    '<w:rFonts w:ascii="Times New Roman"/>','<w:rFonts w:hAnsiTheme="minorHAnsi"/>',
    '<w:rFonts w:cs="Times New Roman"/>','<w:rFonts w:eastAsia="Other"/>',
    '<w:sz w:val="22"/>','<w:szCs w:val="22"/>','<w:vanish/>',
])
@pytest.mark.parametrize('location',['run','style'])
def test_direct_and_assigned_style_override_guards(font,location):
    data=entries()
    if location=='run':data['word/document.xml']=data['word/document.xml'].replace('<w:r>','<w:r><w:rPr>'+font+'</w:rPr>',1)
    else:data['word/styles.xml']=data['word/styles.xml'].replace('</w:rPr>',font+'</w:rPr>',1)
    with pytest.raises(AssertionError):body.assert_docx(zipped(data))


@pytest.mark.parametrize('case',['inherited_break','inherited_theme','character_size','duplicate_style','style_cycle','missing_style'])
def test_style_inheritance_and_character_style_guards(case):
    data=entries()
    if case=='inherited_break':data['word/styles.xml']=data['word/styles.xml'].replace('<w:rPr>','<w:pPr><w:pageBreakBefore/></w:pPr><w:rPr>')
    elif case=='inherited_theme':
        data['word/styles.xml']=data['word/styles.xml'].replace('<w:name','<w:basedOn w:val="Base"/><w:name').replace('</w:styles>',
            '<w:style w:type="paragraph" w:styleId="Base"><w:rPr><w:rFonts w:asciiTheme="minorHAnsi"/></w:rPr></w:style></w:styles>')
    elif case=='character_size':
        data['word/document.xml']=data['word/document.xml'].replace('<w:r>','<w:r><w:rPr><w:rStyle w:val="Wrong"/></w:rPr>',1)
        data['word/styles.xml']=data['word/styles.xml'].replace('</w:styles>',
            '<w:style w:type="character" w:styleId="Wrong"><w:rPr><w:szCs w:val="20"/></w:rPr></w:style></w:styles>')
    elif case=='duplicate_style':data['word/styles.xml']=data['word/styles.xml'].replace('</w:styles>','<w:style w:type="paragraph" w:styleId="Body"/></w:styles>')
    elif case=='style_cycle':data['word/styles.xml']=data['word/styles.xml'].replace('<w:name','<w:basedOn w:val="Body"/><w:name')
    else:data['word/document.xml']=data['word/document.xml'].replace('w:val="Body"','w:val="Missing"')
    with pytest.raises(AssertionError):body.assert_docx(zipped(data))


def test_false_onoff_values_and_consistent_character_override_are_allowed():
    data=entries()
    data['word/document.xml']=data['word/document.xml'].replace('<w:sectPr>','<w:sectPr><w:titlePg w:val="false"/>')
    data['word/settings.xml']=xml('settings','<w:evenAndOddHeaders w:val="0"/>')
    data['word/document.xml']=data['word/document.xml'].replace('<w:r>','<w:r><w:rPr><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>',1)
    assert body.assert_docx(zipped(data))['xml_assertions']=='passed'


@pytest.mark.parametrize('kind',['header','footer'])
@pytest.mark.parametrize('orphan',[False,True])
def test_unreferenced_band_parts_and_relationships_are_rejected(kind,orphan):
    data=entries()
    if orphan:data['word/hidden.xml']=xml('hdr' if kind=='header' else 'ftr',paragraph('Hidden text'))
    else:data['word/_rels/document.xml.rels']=data['word/_rels/document.xml.rels'].replace('</Relationships>',
        f'<Relationship Id="hidden" Type="{body.R[1:-1]}/{kind}" Target="hidden.xml"/></Relationships>')
    with pytest.raises(AssertionError):body.assert_docx(zipped(data))


@pytest.mark.parametrize('case',['extra_page','wrong_page','extra_text','duplicate','missing'])
def test_pdf_page_local_content_is_exact(case):
    values=[body.LITERALS[0],DRAFT,body.LITERALS[1]];pages=[values[:2],values[2:]]
    if case=='extra_page':pages.append([])
    elif case=='wrong_page':pages=[values[:1],values[1:]]
    elif case=='extra_text':pages[0].append('Page 1 of 2')
    elif case=='duplicate':pages[0].append(values[0])
    else:pages[0].pop()
    with pytest.raises(AssertionError):body.assert_pdf(pdf(pages),values)


def test_semantically_wrong_draft_cannot_be_claimed_approved_by_word_count():
    wrong='The organisation has already completed its review of this document.'
    facts=body.assert_docx(changed('word/document.xml',DRAFT,wrong))
    assert facts['draft_semantics']=='pending_reviewer'
    assert 'semantic_review_required' in facts


def staged(tmp_path,*,openxml=True):
    store=ProjectStore(tmp_path);task(store)
    data=zipped(entries());values=[body.LITERALS[0],DRAFT,body.LITERALS[1]]
    operation=store._stage_validated_bytes(path='report.docx',data=data,expected_revision=None,
        idempotency_key='synthetic',task_id='task',tool='word.stage',args={'request':{'tool':'word.create','path':'report.docx','revision':None}},
        validation={'openxml':openxml,'results':[]})
    candidate=store.candidate('report.docx');pdfdata=pdf([values[:2],values[2:]])
    pdfhash=store.put_blob(pdfdata)
    render={'path':'report.docx','revision':candidate['candidate_hash'],'pdf_hash':pdfhash,'pages':2,
            'configuration':{'renderer':'synthetic assertion fixture'}}
    with store.transaction():
        store.db.execute("UPDATE tasks SET status='completed' WHERE id='task'")
        store.db.execute('INSERT INTO render_cache VALUES(?,?,?)',('synthetic',candidate['candidate_hash'],canonical(render)))
    return store,candidate,render,data


def test_exact_candidate_export_save_undo_restore_lifecycle(tmp_path):
    store,candidate,render,data=staged(tmp_path)
    try:
        checks=body.verify_candidate(store,'task',candidate,render)
        assert checks['pdf_export']['source_revision']==digest(data)
        assert not (tmp_path/'report.docx').exists()
        store.apply(candidate['operation_id'],expected_candidate=candidate['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==data and store.candidate('report.docx') is None
        undo=store.undo(candidate['operation_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='remove')
        assert not (tmp_path/'report.docx').exists() and store.candidate('report.docx') is None
        restore=store.undo(undo['id'],'restore');store.apply(restore['id'],expected_candidate=restore['candidate_hash'],action='save')
        assert (tmp_path/'report.docx').read_bytes()==data and store.candidate('report.docx') is None
    finally:store.close()


@pytest.mark.parametrize('case',['worker_validation','revision','path','saved_early','wrong_task'])
def test_candidate_provenance_mismatches_fail_before_export(tmp_path,case):
    store,candidate,render,data=staged(tmp_path,openxml=case!='worker_validation')
    try:
        if case=='revision':render['revision']='0'*64
        elif case=='path':render['path']='another.docx'
        elif case=='saved_early':(tmp_path/'report.docx').write_bytes(data)
        elif case=='wrong_task':
            with store.transaction():store.db.execute("UPDATE operations SET task_id='other'")
        with pytest.raises(AssertionError):body.verify_candidate(store,'task',candidate,render)
        assert store.candidate('report.pdf') is None and not (tmp_path/'report.pdf').exists()
    finally:store.close()
