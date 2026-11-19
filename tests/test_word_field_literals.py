"""Exercise FieldLiteralText through the registered replace_text batch operation.

Dispatch is coordinator-owned. These tests require the integrated worker build.
Unchanged package parts remain byte-identical. Within a changed XML part, the
SDK may normalize numeric character references to UTF-8; complete field XML
(attributes, instructions, runs, formatting and cached content) must remain
canonically identical. SDK-serialized source fixtures also require field-byte
equality. No field content or formatting changes are allowed.
"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

import pytest

from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from test_word_creation import W
from test_word_lists import rewrite
from test_word_page_bands import band_design

FOOTER = 'word/footer1.xml'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
FIELD_XML = rb'<(?:[A-Za-z_][\w.-]*:)?fldSimple\b[^>]*(?:/>|>.*?</(?:[A-Za-z_][\w.-]*:)?fldSimple>)'


def saved(root, *, prefix='Page ', separator=' of ', transform=None):
    store = ProjectStore(root)
    plugin = WordPlugin(store)
    args = band_design()
    args['footer'].update(text=prefix, separator=separator)
    result = plugin.create(**args, idempotency_key='fixture')
    store.apply(result['receipt_id'], expected_candidate=result['candidate_revision'], action='save')
    if transform:
        def change(parts):
            footer = ET.fromstring(parts[FOOTER])
            transform(footer.find('{'+W+'}p'))
            parts[FOOTER] = ET.tostring(footer)
        rewrite(root, (root/'report.docx').read_bytes(), change)
    return store, plugin, (root/'report.docx').read_bytes(), store.file_hash('report.docx')


def target(plugin, revision):
    obj = plugin.list_objects(path='report.docx', revision=revision,
                              parent='footer:1', kind='paragraph')['objects'][0]
    return plugin.read_objects(path='report.docx', revision=revision,
                               ids=[obj['id']])['objects'][0]


def edit(plugin, revision, obj, start, end, text, key='literal'):
    return plugin.stage_batch(path='report.docx', revision=revision, operations=[{
        'id':'literal', 'op':'replace_text', 'target':obj['id'],
        'expected_hash':obj['hash'], 'start':start, 'end':end, 'text':text,
    }], idempotency_key=key)


def canonical(node):
    return ET.canonicalize(ET.tostring(node))


def preserved(before, after, changed_literals, *, sdk_serialized=True):
    with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(io.BytesIO(after)) as new:
        assert set(old.namelist()) == set(new.namelist())
        for name in old.namelist():
            if name != FOOTER:
                assert old.read(name) == new.read(name), name
        a, b = old.read(FOOTER), new.read(FOOTER)
        raw_fields = [re.findall(FIELD_XML, value, re.S) for value in (a, b)]
        assert len(raw_fields[0]) == len(raw_fields[1]) == 2
        if sdk_serialized:
            assert raw_fields[0] == raw_fields[1]
        roots = [ET.fromstring(a), ET.fromstring(b)]
        # Compare complete field subtrees before masking selected literals.
        # Encoding-only normalization must not change attributes, instructions,
        # cached text, run structure or formatting.
        fields = [[canonical(f) for f in root.findall('.//{'+W+'}fldSimple')]
                  for root in roots]
        assert len(fields[0]) == len(fields[1]) == 2
        assert fields[0] == fields[1]
        for root in roots:
            paragraph = root.find('{'+W+'}p')
            literals = [t for run in paragraph.findall('{'+W+'}r') for t in run.findall('{'+W+'}t')]
            for index in changed_literals:
                literals[index].text = ''
                literals[index].attrib.pop(XML_SPACE, None)
        # Only selected literal values and their whitespace-preservation flag may differ.
        assert canonical(roots[0]) == canonical(roots[1])


def test_prefix_separator_exact_replay_noop_preservation_and_undo(tmp_path):
    store, plugin, original, revision = saved(tmp_path)
    try:
        obj = target(plugin, revision)
        assert obj['text'] == 'Page 1 of 1'
        first = edit(plugin, revision, obj, 0, 5, 'Sheet ')
        assert edit(plugin, revision, obj, 0, 5, 'Sheet ') == first
        head = first['candidate_revision']
        updated = target(plugin, head)
        assert updated['text'] == 'Sheet 1 of 1'
        preserved(original, store.blob(head), {0})
        assert edit(plugin, head, updated, 0, 6, 'Sheet ', 'noop')['status'] == 'unchanged'
        start = updated['text'].index(' of ')
        second = edit(plugin, head, updated, start, start+4, ' / ', 'separator')
        assert target(plugin, second['candidate_revision'])['text'] == 'Sheet 1 / 1'
        preserved(original, store.blob(second['candidate_revision']), {0, 1})
        assert (tmp_path/'report.docx').read_bytes() == original
        store.apply(second['receipt_id'], expected_candidate=second['candidate_revision'], action='apply')
        undo = store.undo(second['receipt_id'], 'undo-literals')
        store.apply(undo['id'], expected_candidate=undo['candidate_hash'], action='apply')
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_unicode_offsets_and_numeric_entity_normalization_preserve_complete_fields(tmp_path):
    def change(p):
        # Cache values are display text, not assumed one-digit numbers.
        fields = p.findall('{'+W+'}fldSimple')
        fields[0].find('{'+W+'}r/{'+W+'}t').text = '12\U0001f600'
        fields[1].find('{'+W+'}r/{'+W+'}t').text = '34'
    prefix = '\U0001f4c4 Pág. '
    separator = ' e\u0301 / '
    store, plugin, original, revision = saved(tmp_path, prefix=prefix, separator=separator, transform=change)
    try:
        obj = target(plugin, revision)
        assert obj['text'] == prefix+'12\U0001f600'+separator+'34'
        start = len(prefix)+len('12\U0001f600')
        result = edit(plugin, revision, obj, start, start+len(separator), ' de ')
        assert target(plugin, result['candidate_revision'])['text'] == prefix+'12\U0001f600 de 34'
        updated = store.blob(result['candidate_revision'])
        with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(io.BytesIO(updated)) as new:
            before_fields = re.findall(FIELD_XML, old.read(FOOTER), re.S)
            after_fields = re.findall(FIELD_XML, new.read(FOOTER), re.S)
            assert b'&#128512;' in before_fields[0]
            assert '\U0001f600'.encode('utf-8') in after_fields[0]
            assert before_fields[0] != after_fields[0]  # Encoding changed, not field content.
        preserved(original, updated, {1}, sdk_serialized=False)
    finally:
        store.close()


@pytest.mark.parametrize('start,end,replacement,expected', [
    (1, 1, 'X', 'PXage 1 of 1'),
    (0, 0, 'X', 'XPage 1 of 1'),
    (7, 7, 'X', 'Page 1 Xof 1'),
    (0, 5, '', '1 of 1'),
    (6, 10, '', 'Page 11'),
])
def test_safe_literal_insertions_and_adjacent_deletions(tmp_path, start, end, replacement, expected):
    store, plugin, original, revision = saved(tmp_path)
    try:
        result = edit(plugin, revision, target(plugin, revision), start, end, replacement)
        assert target(plugin, result['candidate_revision'])['text'] == expected
        preserved(original, store.blob(result['candidate_revision']), {0 if start<5 else 1})
    finally:
        store.close()


@pytest.mark.parametrize('start,end', [
    (5, 5), (6, 6), (10, 10), (11, 11),  # Insertions at either field boundary.
    (5, 6), (10, 11), (4, 7), (0, 11),  # Cache edits and crossing replacements.
])
def test_field_boundaries_and_crossing_spans_fail_atomically(tmp_path, start, end):
    store, plugin, original, revision = saved(tmp_path)
    try:
        with pytest.raises(HarnessError, match='field|Field'):
            edit(plugin, revision, target(plugin, revision), start, end, 'X')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_empty_cached_field_is_still_a_barrier(tmp_path):
    def change(p):
        p.find('{'+W+'}fldSimple/{'+W+'}r/{'+W+'}t').text = ''
    store, plugin, original, revision = saved(tmp_path, transform=change)
    try:
        obj = target(plugin, revision)
        assert obj['text'] == 'Page  of 1'
        for index, (start, end) in enumerate([(5, 5), (4, 6)]):
            with pytest.raises(HarnessError, match='field|Field'):
                edit(plugin, revision, obj, start, end, 'X', f'empty-{index}')
        result = edit(plugin, revision, obj, 5, 9, ' / ', 'adjacent-empty')
        assert target(plugin, result['candidate_revision'])['text'] == 'Page  / 1'
        preserved(original, store.blob(result['candidate_revision']), {1}, sdk_serialized=False)
    finally:
        store.close()


def test_split_formatted_literal_runs_preserve_styles_and_true_noop(tmp_path):
    def change(p):
        run = p.find('{'+W+'}r')
        run.find('{'+W+'}t').text = 'Pa'
        props = ET.Element('{'+W+'}rPr'); ET.SubElement(props, '{'+W+'}b'); run.insert(0, props)
        second = ET.Element('{'+W+'}r')
        props = ET.SubElement(second, '{'+W+'}rPr'); ET.SubElement(props, '{'+W+'}i')
        ET.SubElement(second, '{'+W+'}t', {XML_SPACE:'preserve'}).text = 'ge '
        p.insert(list(p).index(run)+1, second)
    store, plugin, original, revision = saved(tmp_path, transform=change)
    try:
        obj = target(plugin, revision)
        assert edit(plugin, revision, obj, 0, 5, 'Page ', 'same')['status'] == 'unchanged'
        assert store.candidate('report.docx') is None
        result = edit(plugin, revision, obj, 1, 4, 'aper', 'split')
        assert target(plugin, result['candidate_revision'])['text'] == 'Paper 1 of 1'
        preserved(original, store.blob(result['candidate_revision']), {0, 1}, sdk_serialized=False)
    finally:
        store.close()


@pytest.mark.parametrize('structure', [
    'unsupported_code', 'field_switch', 'nested_field', 'complex_field', 'hyperlink',
    'drawing', 'extension', 'property_extension', 'cached_tab', 'literal_break',
    'tracked_properties', 'field_attribute',
])
def test_unsupported_structures_are_not_flattened(tmp_path, structure):
    def change(p):
        field = p.find('{'+W+'}fldSimple')
        run = p.find('{'+W+'}r')
        if structure == 'unsupported_code':
            field.set('{'+W+'}instr', ' DATE ')
        elif structure == 'field_switch':
            field.set('{'+W+'}instr', ' PAGE \\* ROMAN ')
        elif structure == 'nested_field':
            ET.SubElement(field, '{'+W+'}fldSimple', {'{'+W+'}instr':' PAGE '})
        elif structure == 'complex_field':
            ET.SubElement(run, '{'+W+'}fldChar', {'{'+W+'}fldCharType':'begin'})
        elif structure == 'hyperlink':
            ET.SubElement(p, '{'+W+'}hyperlink')
        elif structure == 'drawing':
            ET.SubElement(run, '{'+W+'}drawing')
        elif structure == 'extension':
            ET.SubElement(field, '{urn:unsupported-field}extra')
        elif structure == 'property_extension':
            props = ET.Element('{'+W+'}rPr'); ET.SubElement(props, '{urn:unsupported-field}extra'); run.insert(0, props)
        elif structure == 'cached_tab':
            ET.SubElement(field.find('{'+W+'}r'), '{'+W+'}tab')
        elif structure == 'literal_break':
            ET.SubElement(run, '{'+W+'}br')
        elif structure == 'tracked_properties':
            props = ET.Element('{'+W+'}rPr'); ET.SubElement(props, '{'+W+'}rPrChange'); run.insert(0, props)
        else:
            field.set('{urn:unsupported-field}flag', '1')
    store, plugin, original, revision = saved(tmp_path, transform=change)
    try:
        with pytest.raises(HarnessError):
            edit(plugin, revision, target(plugin, revision), 0, 4, 'Sheet')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_no_literal_text_fails_closed(tmp_path):
    def change(p):
        for run in list(p.findall('{'+W+'}r')):
            p.remove(run)
    store, plugin, original, revision = saved(tmp_path, transform=change)
    try:
        obj = target(plugin, revision)
        assert obj['text'] == '11'
        with pytest.raises(HarnessError):
            edit(plugin, revision, obj, 0, 0, 'prefix')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_stale_hash_and_revision_refused_without_losing_candidate(tmp_path):
    store, plugin, original, revision = saved(tmp_path)
    try:
        obj = target(plugin, revision)
        with pytest.raises(HarnessError, match='stale_object'):
            edit(plugin, revision, {**obj, 'hash':'0'*64}, 0, 4, 'Sheet', 'bad-hash')
        assert store.candidate('report.docx') is None
        result = edit(plugin, revision, obj, 0, 4, 'Sheet', 'valid')
        candidate = store.candidate('report.docx')
        with pytest.raises(HarnessError, match='revision|Revision'):
            edit(plugin, revision, obj, 0, 4, 'Other', 'old-revision')
        assert store.candidate('report.docx') == candidate
        assert candidate['candidate_hash'] == result['candidate_revision']
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()


def test_field_crossing_refusal_discards_prior_header_change_in_same_batch(tmp_path):
    store, plugin, original, revision = saved(tmp_path)
    try:
        footer = target(plugin, revision)
        header = plugin.list_objects(path='report.docx', revision=revision,
                                     parent='header:1', kind='paragraph')['objects'][0]
        with pytest.raises(HarnessError, match='field|Field'):
            plugin.stage_batch(path='report.docx', revision=revision, operations=[
                {'id':'header', 'op':'replace_text', 'target':header['id'], 'expected_hash':header['hash'],
                 'start':0, 'end':9, 'text':'Annual'},
                {'id':'footer', 'op':'replace_text', 'target':footer['id'], 'expected_hash':footer['hash'],
                 'start':4, 'end':7, 'text':'X'},
            ], idempotency_key='atomic')
        assert store.candidate('report.docx') is None
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()
