"""Local, bounded UNO pagination configuration; source DOCX is never rewritten."""
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from ..errors import HarnessError
from ..storage import digest

ROOT=Path(__file__).resolve().parents[3]
OFFICE=Path('/Applications/LibreOffice.app/Contents')
NS='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def instructions(data):
    result=[]
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        for name in package.namelist():
            if not name.startswith('word/') or not name.endswith('.xml'):continue
            document=ET.fromstring(package.read(name));stack=[]
            for node in document.iter():
                if node.tag==NS+'fldSimple':result.append(node.get(NS+'instr',''))
                elif node.tag==NS+'fldChar':
                    kind=node.get(NS+'fldCharType')
                    if kind=='begin':stack.append([])
                    elif kind=='separate' and stack:
                        result.append(''.join(stack[-1]));stack[-1]=[]
                    elif kind=='end' and stack:result.append(''.join(stack.pop()))
                elif node.tag==NS+'instrText' and stack:stack[-1].append(node.text or '')
            result.extend(''.join(parts) for parts in stack)
    return [code for code in result if code.strip()]


def contains_toc(data):
    return any(re.match(r'\s*TOC\b',code,re.IGNORECASE) for code in instructions(data))


def contains_page_fields(data):
    return any(re.match(r'\s*(PAGE|NUMPAGES)\b',code,re.IGNORECASE) for code in instructions(data))


def adapter(data):
    codes=instructions(data)
    # A refresh may evaluate fields. Qualify only baseline, local field types;
    # macros and linked-resource updates are also disabled in the UNO loader.
    for code in codes:
        match=re.match(r'\s*(\w+)\b',code)
        if not match or match[1].upper() not in {'TOC','PAGE','NUMPAGES','PAGEREF','HYPERLINK'}:
            raise HarnessError('field_update_unsupported','Field updating requires baseline page, TOC or hyperlink fields only.')
    homes=list((ROOT/'.runtime/java').glob('*/Contents/Home'))
    worker=ROOT/'workers/fields/classes/UpdateFields.class'
    jar=OFFICE/'Resources/java/libreoffice.jar'
    soffice=OFFICE/'MacOS/soffice'
    if len(homes)!=1 or not (homes[0]/'bin/java').is_file() or not worker.is_file() or not jar.is_file() or not soffice.is_file():
        raise HarnessError('field_worker_missing','Build the local Java field worker before refreshing document fields.')
    prefix=[str(homes[0]/'bin/java'),'-Xmx256m',f'-Djava.library.path={OFFICE}/Frameworks',
            '-cp',str(worker.parent)+':'+str(jar),'UpdateFields',str(soffice)]
    config={'worker_sha256':digest(worker.read_bytes()),'uno_sha256':digest(jar.read_bytes()),
            'java_release_sha256':digest((homes[0]/'release').read_bytes())}
    return prefix,config
