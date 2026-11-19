import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.contracts import ParagraphFormat
from test_word_worker import fixture_docx,W


def test_sparse_paragraph_format_preserves_text_runs_parts_and_undo(tmp_path):
    source=tmp_path/'a.docx';fixture_docx(source);original=source.read_bytes()
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('a.docx')
        before=plugin.read_objects(path='a.docx',revision=revision,ids=['paragraph:1','paragraph:2'],include_format=True)
        first=before['objects'][0]
        assert first['direct_format']['runs'][0]['bold'] is True
        assert first['direct_format']['runs'][1]['italic'] is True
        properties={'alignment':'center','spacing_after_twips':240,'first_line_twips':-360,
                    'keep_with_next':True,'keep_lines':True,'page_break_before':False,
                    'font':'Arial','size_half_points':30,'color':'17365D','bold':False}
        op={'id':'format','op':'format_paragraph','target':first['id'],'expected_hash':first['hash'],'properties':properties}
        result=plugin.stage_batch(path='a.docx',revision=revision,operations=[op],idempotency_key='format')
        assert source.read_bytes()==original
        after=plugin.read_objects(path='a.docx',revision=result['candidate_revision'],ids=['paragraph:1','paragraph:2'],include_format=True)
        first_after,second_after=after['objects']
        assert first_after['text']==first['text'] and second_after==before['objects'][1]
        fmt=first_after['direct_format']
        assert fmt['properties']['alignment']=='center' and fmt['properties']['hanging_twips']=='360'
        assert fmt['properties']['page_break_before'] is False
        assert fmt['runs'][1]['italic'] is True
        for run in fmt['runs']:
            assert run['bold'] is False and run['size_half_points']=='30' and run['font']=='Arial'
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            olddoc=ET.fromstring(old.read('word/document.xml'));newdoc=ET.fromstring(new.read('word/document.xml'))
            assert [e.text for e in olddoc.iter('{'+W+'}t')]==[e.text for e in newdoc.iter('{'+W+'}t')]
            assert len(list(olddoc.iter('{'+W+'}r')))==len(list(newdoc.iter('{'+W+'}r')))
        no_op=plugin.stage_batch(path='a.docx',revision=result['candidate_revision'],operations=[{**op,'expected_hash':first_after['hash']}],idempotency_key='same-values')
        assert no_op['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo-format');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert source.read_bytes()==original
    finally:store.close()


def test_spacing_only_leaves_run_properties_and_field_markup_intact(tmp_path):
    source=tmp_path/'a.docx';fixture_docx(source,field=True)
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('a.docx');first=plugin.list_objects(path='a.docx',revision=revision)['objects'][0]
        result=plugin.stage_batch(path='a.docx',revision=revision,operations=[{'id':'space','op':'format_paragraph','target':first['id'],'expected_hash':first['hash'],'properties':{'spacing_before_twips':120}}],idempotency_key='space')
        with zipfile.ZipFile(source) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            a=ET.fromstring(old.read('word/document.xml'));b=ET.fromstring(new.read('word/document.xml'))
            assert [ET.tostring(r) for r in a.iter('{'+W+'}r')]==[ET.tostring(r) for r in b.iter('{'+W+'}r')]
            assert ET.tostring(a.find('.//{'+W+'}fldSimple'))==ET.tostring(b.find('.//{'+W+'}fldSimple'))
        default=plugin.read_objects(path='a.docx',revision=result['candidate_revision'],ids=['paragraph:1'])
        assert 'direct_format' not in default['objects'][0]
    finally:store.close()


def test_format_inspection_is_bounded_and_font_override_removes_theme(tmp_path):
    source=tmp_path/'a.docx';fixture_docx(source)
    with zipfile.ZipFile(source) as package:parts={n:package.read(n) for n in package.namelist()}
    doc=ET.fromstring(parts['word/document.xml']);paragraph=doc.find('.//{'+W+'}p')
    first_run=paragraph.find('{'+W+'}r');props=first_run.find('{'+W+'}rPr')
    props.insert(0,ET.Element('{'+W+'}rFonts',{'{'+W+'}asciiTheme':'majorHAnsi'}))
    for _ in range(20):ET.SubElement(ET.SubElement(paragraph,'{'+W+'}r'),'{'+W+'}t').text='x'
    parts['word/document.xml']=ET.tostring(doc)
    with zipfile.ZipFile(source,'w') as package:
        for name,data in parts.items():package.writestr(name,data)
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        revision=store.file_hash('a.docx');first=plugin.read_objects(path='a.docx',revision=revision,ids=['paragraph:1'],include_format=True)['objects'][0]
        assert first['direct_format']['total_runs']==22
        assert first['direct_format']['runs_truncated'] and len(first['direct_format']['runs'])==16
        result=plugin.stage_batch(path='a.docx',revision=revision,operations=[{'id':'font','op':'format_paragraph','target':'paragraph:1','expected_hash':first['hash'],'properties':{'font':'Arial'}}],idempotency_key='font')
        with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as package:
            result_doc=ET.fromstring(package.read('word/document.xml'))
            fonts=result_doc.findall('.//{'+W+'}rFonts')
            assert len(fonts)==22
            assert all(f.get('{'+W+'}ascii')=='Arial' and f.get('{'+W+'}asciiTheme') is None for f in fonts)
    finally:store.close()


@pytest.mark.parametrize('values',[{}, {'alignment':'diagonal'}, {'font':''}, {'color':'blue'}, {'size_half_points':0}, {'first_line_twips':-99999}, {'bold':'false'}, {'script':'print(1)'}])
def test_format_contract_rejects_ambiguous_or_oversized_values(values):
    with pytest.raises(ValidationError):ParagraphFormat(**values)
