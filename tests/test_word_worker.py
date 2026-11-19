"""Real .NET worker checks on isolated DOCX packages, without model doubles."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile
from xml.etree import ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[1]
DOTNET = ROOT/'.runtime/dotnet/dotnet'
WORKER = ROOT/'workers/openxml/bin/Debug/net10.0/LocalOffice.OpenXml.dll'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def fixture_docx(path, *, field=False):
    parts = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        'word/styles.xml': f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>',
        'word/document.xml': f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>A😀BC</w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>DEFG</w:t></w:r>{"<w:fldSimple w:instr=\"DATE\"><w:r><w:t>date</w:t></w:r></w:fldSimple>" if field else ""}</w:p><w:p><w:r><w:t>Unchanged paragraph</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
    }
    with zipfile.ZipFile(path, 'w') as z:
        for name, content in parts.items(): z.writestr(name, content)


def invoke(path, action, arguments, *, revision=None, output=None):
    request = {'input': str(path), 'action': action, 'arguments': arguments,
               'revision': revision or hashlib.sha256(path.read_bytes()).hexdigest()}
    if output: request['output'] = str(output)
    env = {**os.environ, 'DOTNET_CLI_TELEMETRY_OPTOUT':'1', 'DOTNET_GENERATE_ASPNET_CERTIFICATE':'false'}
    proc = subprocess.run([str(DOTNET), str(WORKER)], input=json.dumps(request),
                          capture_output=True, text=True, timeout=15, env=env)
    result = json.loads(proc.stdout)
    assert (proc.returncode == 0) == result['ok']
    return result


@pytest.fixture
def source(tmp_path):
    assert DOTNET.exists() and WORKER.exists(), 'Build the pinned Open XML worker before running worker tests'
    path=tmp_path/'source.docx';fixture_docx(path);return path


def test_scoped_index_read_and_stale_revision(source):
    original=source.read_bytes()
    first=invoke(source,'list',{'limit':1})['result']
    assert len(first['objects'])==1 and first['next_cursor']
    second=invoke(source,'list',{'limit':1,'cursor':first['next_cursor']})['result']
    assert second['objects'][0]['id']=='paragraph:2'
    read=invoke(source,'read',{'ids':['paragraph:1'],'offset':1,'max_chars':2})['result']['objects'][0]
    assert read['text']=='😀B' and read['next_offset']==3
    assert not invoke(source,'read',{'ids':['paragraph:1']},revision='0'*64)['ok']
    assert source.read_bytes()==original


def test_compact_edit_preserves_runs_parts_and_original(source,tmp_path):
    original=source.read_bytes()
    objects=invoke(source,'list',{})['result']['objects']
    output=tmp_path/'candidate.docx'
    op={'id':'e1','op':'replace_text','target':'paragraph:1','expected_hash':objects[0]['hash'],
        'start':1,'end':6,'text':'New'}
    result=invoke(source,'patch',{'operations':[op]},output=output)
    assert result['ok'],result
    assert source.read_bytes()==original
    read=invoke(output,'read',{'ids':['paragraph:1','paragraph:2']})['result']['objects']
    assert read[0]['text']=='ANewFG' and read[1]['text']=='Unchanged paragraph'
    with zipfile.ZipFile(source) as before,zipfile.ZipFile(output) as after:
        assert set(before.namelist())==set(after.namelist())
        for name in before.namelist():
            if name!='word/document.xml': assert before.read(name)==after.read(name)
        doc=ET.fromstring(after.read('word/document.xml'))
        assert doc.find('.//{'+W+'}b') is not None and doc.find('.//{'+W+'}i') is not None
    assert result['result']['candidate_revision']==hashlib.sha256(output.read_bytes()).hexdigest()


def test_invalid_second_edit_publishes_no_partial_candidate(source,tmp_path):
    objects=invoke(source,'list',{})['result']['objects']
    operations=[{'id':'e1','op':'set_style','target':'paragraph:1','expected_hash':objects[0]['hash'],'style_id':'Heading1'},
                {'id':'e2','op':'set_style','target':'paragraph:2','expected_hash':objects[1]['hash'],'style_id':'Missing'}]
    original=source.read_bytes();output=tmp_path/'candidate.docx'
    result=invoke(source,'patch',{'operations':operations},output=output)
    assert not result['ok'] and not output.exists() and source.read_bytes()==original


def test_style_change_and_unsupported_field_fail_closed(source,tmp_path):
    first=invoke(source,'list',{})['result']['objects'][0]
    output=tmp_path/'style.docx'
    result=invoke(source,'patch',{'operations':[{'id':'e1','op':'set_style','target':first['id'],
                       'expected_hash':first['hash'],'style_id':'Heading1'}]},output=output)
    assert result['ok'],result
    with zipfile.ZipFile(output) as z:
        assert b'Heading1' in z.read('word/document.xml')
    field=tmp_path/'field.docx';fixture_docx(field,field=True)
    first=invoke(field,'list',{})['result']['objects'][0]
    result=invoke(field,'patch',{'operations':[{'id':'e1','op':'replace_text','target':first['id'],
        'expected_hash':first['hash'],'start':0,'end':1,'text':'Z'}]},output=tmp_path/'rejected.docx')
    assert not result['ok'] and not (tmp_path/'rejected.docx').exists()
