"""Registered-operation coverage; requires coordinator's dispatch/contracts and worker build."""
import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from test_word_image_format import placed, read, edit
from test_word_images import W, WP
from test_word_lists import rewrite

FLOAT = dict(mode='floating', wrap='square', horizontal_origin='margin',
             vertical_origin='paragraph', horizontal_offset_inches=0.25,
             vertical_offset_inches=0.125)


def layout(plugin, revision, properties, key='layout', target=None):
    target = target or read(plugin, revision)
    return plugin.stage_batch(path='report.docx', revision=revision, operations=[{
        'id':'layout', 'op':'set_image_layout', 'target':target['id'],
        'expected_hash':target['hash'], 'properties':properties}], idempotency_key=key)


def tree(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as package:
        return ET.fromstring(package.read('word/document.xml'))


def frame(blob):
    return tree(blob).find('.//{'+W+'}drawing')[0]


def canonical(node):
    return ET.canonicalize(ET.tostring(node))


def preserved(before, after):
    with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(io.BytesIO(after)) as new:
        assert set(old.namelist()) == set(new.namelist())
        for name in old.namelist():
            if name != 'word/document.xml':
                assert old.read(name) == new.read(name), name
    a, b = tree(before), tree(after)
    for root in (a, b):
        drawing = root.find('.//{'+W+'}drawing')
        container = drawing[0]
        # These contents, including IDs, crop, extent and alt text, are immutable.
        retained = [canonical(c) for c in container if c.tag.split('}')[-1] in
                    ('extent','effectExtent','docPr','cNvGraphicFramePr','graphic')]
        if root is a:
            expected = retained
        else:
            assert retained == expected
        drawing.remove(container)
    assert canonical(a) == canonical(b)


def test_float_sparse_wrap_roundtrip_noop_preservation_and_exact_undo(tmp_path):
    store, plugin, original, rev = placed(tmp_path)
    try:
        cropped = edit(plugin, rev, read(plugin, rev), {'crop_left_percent':12.5})
        store.apply(cropped['receipt_id'], expected_candidate=cropped['candidate_revision'], action='apply')
        original = (tmp_path/'report.docx').read_bytes(); rev = cropped['candidate_revision']
        target = read(plugin, rev)
        first = layout(plugin, rev, FLOAT, target=target)
        assert layout(plugin, rev, FLOAT, target=target) == first
        head = first['candidate_revision']; floating = store.blob(head)
        preserved(original, floating)
        f = frame(floating)
        assert f.tag == '{'+WP+'}anchor'
        assert f.find('{'+WP+'}positionH').attrib['relativeFrom'] == 'margin'
        assert f.find('{'+WP+'}positionH/{'+WP+'}posOffset').text == '228600'
        assert layout(plugin, head, FLOAT, 'noop')['status'] == 'unchanged'
        sparse = layout(plugin, head, {'vertical_offset_inches':-0.5}, 'sparse')
        new = frame(store.blob(sparse['candidate_revision']))
        assert canonical(new.find('{'+WP+'}positionH')) == canonical(f.find('{'+WP+'}positionH'))
        assert new.find('{'+WP+'}positionV/{'+WP+'}posOffset').text == '-457200'
        head = sparse['candidate_revision']
        for wrap, tag in [('top_bottom','wrapTopAndBottom'), ('none','wrapNone'), ('square','wrapSquare')]:
            result = layout(plugin, head, {'wrap':wrap}, wrap)
            preserved(original, store.blob(result['candidate_revision']))
            head = result['candidate_revision']
            assert frame(store.blob(head)).find('{'+WP+'}'+tag) is not None
        back = layout(plugin, head, {'mode':'inline'}, 'back')
        assert frame(store.blob(back['candidate_revision'])).tag == '{'+WP+'}inline'
        preserved(original, store.blob(back['candidate_revision']))
        assert layout(plugin, back['candidate_revision'], {'mode':'inline'}, 'inline-noop')['status'] == 'unchanged'
        assert (tmp_path/'report.docx').read_bytes() == original
        store.apply(back['receipt_id'], expected_candidate=back['candidate_revision'], action='apply')
        undo = store.undo(back['receipt_id'], 'undo-layout')
        store.apply(undo['id'], expected_candidate=undo['candidate_hash'], action='apply')
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


@pytest.mark.parametrize('horizontal', ['page','margin','column'])
@pytest.mark.parametrize('vertical', ['page','margin','paragraph'])
def test_origins_and_explicit_offsets(tmp_path, horizontal, vertical):
    store, plugin, original, rev = placed(tmp_path)
    try:
        result = layout(plugin, rev, dict(FLOAT, horizontal_origin=horizontal,
                        vertical_origin=vertical, horizontal_offset_inches=-22, vertical_offset_inches=22))
        f = frame(store.blob(result['candidate_revision']))
        for axis, origin, offset in [('H',horizontal,-20116800), ('V',vertical,20116800)]:
            p = f.find('{'+WP+'}position'+axis)
            assert p.attrib['relativeFrom'] == origin
            assert int(p[0].text) == offset
        preserved(original, store.blob(result['candidate_revision']))
    finally:
        store.close()


@pytest.mark.parametrize('props', [{}, {'mode':'floating'}, {'wrap':'square'},
    dict(FLOAT, horizontal_offset_inches=22.1), dict(FLOAT, vertical_origin='line'),
    dict(FLOAT, wrap='tight'), {'mode':'inline','wrap':'none'},
    dict(FLOAT, horizontal_offset_inches=None)])
def test_invalid_requests_leave_original_and_candidate_unchanged(tmp_path, props):
    store, plugin, original, rev = placed(tmp_path)
    try:
        before = store.candidate('report.docx')
        with pytest.raises((HarnessError, ValidationError)):
            layout(plugin, rev, props)
        assert store.candidate('report.docx') == before
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_stale_hash_and_wrong_target_refused(tmp_path):
    store, plugin, original, rev = placed(tmp_path)
    try:
        target = read(plugin, rev)
        with pytest.raises(HarnessError, match='stale_object'):
            layout(plugin, rev, FLOAT, target=dict(target, hash='0'*64))
        target = plugin.read_objects(path='report.docx', revision=rev, ids=['paragraph:2'])['objects'][0]
        with pytest.raises(HarnessError, match='image target'):
            layout(plugin, rev, FLOAT, target=target)
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


@pytest.mark.parametrize('mutation', ['aligned','relative','linked','grouped'])
def test_unsupported_layouts_refused_without_flattening(tmp_path, mutation):
    store, plugin, original, rev = placed(tmp_path)
    try:
        result = layout(plugin, rev, FLOAT)
        store.apply(result['receipt_id'], expected_candidate=result['candidate_revision'], action='apply')
        def change(parts):
            root = ET.fromstring(parts['word/document.xml'])
            f = root.find('.//{'+WP+'}anchor')
            if mutation == 'aligned':
                p = f.find('{'+WP+'}positionH'); p.remove(p[0]); ET.SubElement(p,'{'+WP+'}align').text='center'
            elif mutation == 'relative':
                ET.SubElement(f,'{http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing}sizeRelH')
            elif mutation == 'linked':
                blip = root.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
                ns = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
                blip.set(ns+'link', blip.attrib.pop(ns+'embed'))
            else:
                drawing = root.find('.//{'+W+'}drawing'); drawing.append(ET.fromstring(ET.tostring(f)))
            parts['word/document.xml'] = ET.tostring(root)
        rewrite(tmp_path, (tmp_path/'report.docx').read_bytes(), change)
        rev = store.file_hash('report.docx'); original = (tmp_path/'report.docx').read_bytes()
        with pytest.raises(HarnessError):
            layout(plugin, rev, {'horizontal_offset_inches':1}, 'unsupported')
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_existing_anchor_flags_distances_and_wrap_side_survive_sparse_edit(tmp_path):
    store, plugin, original, rev = placed(tmp_path)
    try:
        result = layout(plugin, rev, FLOAT)
        store.apply(result['receipt_id'], expected_candidate=result['candidate_revision'], action='apply')
        def change(parts):
            root = ET.fromstring(parts['word/document.xml'])
            f = root.find('.//{'+WP+'}anchor')
            f.attrib.update(behindDoc='1', relativeHeight='1234', allowOverlap='0', distL='12700')
            f.find('{'+WP+'}wrapSquare').set('wrapText', 'largest')
            parts['word/document.xml'] = ET.tostring(root)
        rewrite(tmp_path, (tmp_path/'report.docx').read_bytes(), change)
        rev = store.file_hash('report.docx'); original = (tmp_path/'report.docx').read_bytes()
        result = layout(plugin, rev, {'horizontal_offset_inches':1}, 'existing')
        updated = store.blob(result['candidate_revision']); f = frame(updated)
        assert f.attrib == frame(original).attrib
        assert f.find('{'+WP+'}wrapSquare').attrib['wrapText'] == 'largest'
        preserved(original, updated)
        assert layout(plugin, result['candidate_revision'], {'wrap':'square'}, 'same-square')['status'] == 'unchanged'
    finally:
        store.close()


@pytest.mark.parametrize('hidden', ['1', 'true'])
def test_hidden_anchor_refuses_inline_conversion_but_preserves_sparse_edits(tmp_path, hidden):
    store, plugin, original, rev = placed(tmp_path)
    try:
        result = layout(plugin, rev, FLOAT)
        store.apply(result['receipt_id'], expected_candidate=result['candidate_revision'], action='apply')
        def change(parts):
            root = ET.fromstring(parts['word/document.xml'])
            root.find('.//{'+WP+'}anchor').set('hidden', hidden)
            parts['word/document.xml'] = ET.tostring(root)
        rewrite(tmp_path, (tmp_path/'report.docx').read_bytes(), change)
        rev = store.file_hash('report.docx'); original = (tmp_path/'report.docx').read_bytes()
        target = read(plugin, rev)
        before = store.candidate('report.docx')
        with pytest.raises(HarnessError, match='Hidden floating images'):
            layout(plugin, rev, {'mode':'inline'}, 'hidden-inline', target=target)
        assert store.candidate('report.docx') == before
        assert (tmp_path/'report.docx').read_bytes() == original
        result = layout(plugin, rev, {'horizontal_offset_inches':1}, 'hidden-sparse', target=target)
        updated = store.blob(result['candidate_revision'])
        assert frame(updated).attrib['hidden'] == hidden
        preserved(original, updated)
    finally:
        store.close()


def test_composite_graphic_reports_unsupported_and_refuses_edit(tmp_path):
    store, plugin, original, rev = placed(tmp_path)
    try:
        def change(parts):
            root = ET.fromstring(parts['word/document.xml'])
            data = root.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}graphicData')
            ET.SubElement(data, '{urn:localoffice:unsupported-graphic}extra')
            parts['word/document.xml'] = ET.tostring(root)
        rewrite(tmp_path, original, change)
        rev = store.file_hash('report.docx'); original = (tmp_path/'report.docx').read_bytes()
        target = read(plugin, rev)
        description = target['direct_format']['layout']
        assert description['supported'] is False
        assert 'Composite graphic content' in description['reason']
        before = store.candidate('report.docx')
        with pytest.raises(HarnessError, match='Composite graphic content'):
            layout(plugin, rev, FLOAT, 'composite', target=target)
        assert store.candidate('report.docx') == before
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()
