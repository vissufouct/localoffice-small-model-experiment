"""New-document first/even stories; no imported-section linking claims."""
import io
import zipfile
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from localoffice.storage import ProjectStore
from localoffice.word.contracts import CreateDocument
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer
from test_word_creation import W

VARIANTS = ('first_header', 'first_footer', 'even_header', 'even_footer')
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def variant_design():
    args = {'path': 'variants.docx', 'styles': [{'id': 'Band', 'font': 'Arial', 'size_points': 9}],
            'blocks': [{'kind': 'paragraph', 'text': f'Body page {i}', 'page_break_before': i > 1}
                       for i in range(1, 4)]}
    for name in ('header', 'footer', *VARIANTS):
        args[name] = {'text': name + ' ', 'style': 'Band', 'alignment': 'right' if 'header' in name else 'center',
                      'page_numbers': 'current_total', 'separator': ' of '}
    return args


def stories(data):
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        document = ET.fromstring(package.read('word/document.xml'))
        section = document.find('.//{' + W + '}sectPr')
        rels = {r.get('Id'): r for r in ET.fromstring(package.read('word/_rels/document.xml.rels'))}
        result = {}
        for kind in ('header', 'footer'):
            for ref in section.findall('{' + W + '}' + kind + 'Reference'):
                rel = rels[ref.get('{' + R + '}id')]
                assert rel.get('Type').endswith('/' + kind)
                assert rel.get('TargetMode') != 'External'
                target = rel.get('Target').lstrip('/')
                if not target.startswith('word/'):
                    target = 'word/' + target
                result[(kind, ref.get('{' + W + '}type'))] = ET.fromstring(package.read(target))
        settings = ET.fromstring(package.read('word/settings.xml')) if 'word/settings.xml' in package.namelist() else None
        return section, settings, result


def test_all_variant_relationships_fields_and_restart_replay_undo(tmp_path):
    args = variant_design()
    store = ProjectStore(tmp_path)
    try:
        receipt = WordPlugin(store).create(**args, idempotency_key='variants')
        data = store.blob(receipt['candidate_revision'])
        section, settings, bands = stories(data)
        assert len(bands) == 6
        assert section.find('{' + W + '}titlePg').get('{' + W + '}val') in ('true', '1')
        assert settings.find('{' + W + '}evenAndOddHeaders').get('{' + W + '}val') in ('true', '1')
        assert settings.find('{' + W + '}updateFields') is not None
        for (kind, variant), band in bands.items():
            name = kind if variant == 'default' else variant + '_' + kind
            assert band.find('.//{' + W + '}t').text == name + ' '
            assert band.find('.//{' + W + '}pStyle').get('{' + W + '}val') == 'Band'
            assert band.find('.//{' + W + '}jc').get('{' + W + '}val') == args[name]['alignment']
            fields = band.findall('.//{' + W + '}fldSimple')
            assert [f.get('{' + W + '}instr').strip() for f in fields] == ['PAGE', 'NUMPAGES']
            assert all(f.get('{' + W + '}dirty') in ('true', '1') for f in fields)
        assert not (tmp_path / args['path']).exists()
    finally:
        store.close()
    store = ProjectStore(tmp_path)
    try:
        assert WordPlugin(store).create(**args, idempotency_key='variants') == receipt
        store.apply(receipt['receipt_id'], expected_candidate=receipt['candidate_revision'], action='save')
        undo = store.undo(receipt['receipt_id'], 'remove')
        store.apply(undo['id'], expected_candidate=undo['candidate_hash'], action='remove')
        restore = store.undo(undo['id'], 'restore')
        store.apply(restore['id'], expected_candidate=restore['candidate_hash'], action='save')
        assert (tmp_path / args['path']).read_bytes() == data
    finally:
        store.close()


@pytest.mark.parametrize('name', VARIANTS + (None,))
def test_variant_switches_only_when_present_and_no_implicit_counterpart(tmp_path, name):
    args = {'path': 'single.docx', 'blocks': [{'kind': 'paragraph', 'text': 'Body'}],
            'header': {'text': 'Default header'}, 'footer': {'text': 'Default footer'}}
    if name:
        args[name] = {}  # An explicitly empty variant still enables its switch.
    store = ProjectStore(tmp_path)
    try:
        result = WordPlugin(store).create(**args, idempotency_key='single')
        section, settings, bands = stories(store.blob(result['candidate_revision']))
        assert (section.find('{' + W + '}titlePg') is not None) == bool(name and name.startswith('first'))
        assert (settings.find('{' + W + '}evenAndOddHeaders') is not None) == bool(name and name.startswith('even'))
        expected = {('header', 'default'), ('footer', 'default')}
        if name:
            variant, kind = name.split('_')
            expected.add((kind, variant))
        assert set(bands) == expected
    finally:
        store.close()


@pytest.mark.parametrize('name', VARIANTS)
@pytest.mark.parametrize('band', [
    {'style': 'Missing'}, {'text': 'bad\x01'}, {'text': 'x' * 2001},
    {'separator': 'x' * 41}, {'alignment': 'both'}, {'page_numbers': 'arbitrary'},
])
def test_variants_preserve_style_text_field_limits(name, band):
    with pytest.raises(ValidationError):
        CreateDocument(path='a.docx', blocks=[{'kind': 'paragraph', 'text': 'Body'}], **{name: band})


@pytest.mark.asyncio
async def test_render_three_pages_select_first_even_default_and_update_fields(tmp_path):
    store = ProjectStore(tmp_path)
    try:
        receipt = WordPlugin(store).create(**variant_design(), idempotency_key='render')
        rendered = await WordRenderer(store).render(path='variants.docx', revision=receipt['candidate_revision'])
        pdf = PdfReader(io.BytesIO(store.blob(rendered['pdf_hash'])))
        assert len(pdf.pages) == 3
        for i, (page, prefix) in enumerate(zip(pdf.pages, ('first_', 'even_', '')), 1):
            text = ' '.join(page.extract_text().split())
            assert f'Body page {i}' in text
            for kind in ('header', 'footer'):
                assert f'{prefix}{kind} {i} of 3' in text
            for other in ('first_', 'even_'):
                if other != prefix:
                    assert other + 'header' not in text
                    assert other + 'footer' not in text
    finally:
        store.close()


@pytest.mark.parametrize('name', VARIANTS)
def test_variant_shape_accepts_inline_reference_for_coordinator_binding(name):
    request = CreateDocument(path='a.docx', blocks=[{'kind': 'paragraph', 'text': 'Body'}],
                             **{name: {'text': {'ref': 'ct_' + 'a' * 24}}})
    assert getattr(request, name).text.ref == 'ct_' + 'a' * 24
