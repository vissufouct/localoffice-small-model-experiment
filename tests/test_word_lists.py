import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.errors import HarnessError
from localoffice.word.contracts import SetList
from pydantic import ValidationError
from test_word_tables import saved_table
from test_word_creation import W


def get(plugin,revision,id,format=True):
    return plugin.read_objects(path='report.docx',revision=revision,ids=[id],include_format=format)['objects'][0]


def apply_list(plugin,revision,target,number,level=0,key='list'):
    return plugin.stage_batch(path='report.docx',revision=revision,operations=[{'id':'list','op':'set_list','target':target['id'],'expected_hash':target['hash'],'number_id':number,'level':level}],idempotency_key=key)


def rewrite(root,data,transform):
    with zipfile.ZipFile(io.BytesIO(data)) as z:parts={n:z.read(n) for n in z.namelist()}
    transform(parts)
    with zipfile.ZipFile(root/'report.docx','w') as z:
        for name,value in parts.items():z.writestr(name,value)


def test_list_discovery_application_preservation_and_undo(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        entries=plugin.list_objects(path='report.docx',revision=revision,kind='numbering',limit=1)
        assert entries['objects'][0]['id']=='numbering:1' and 'bullet' in entries['objects'][0]['preview']
        next_page=plugin.list_objects(path='report.docx',revision=revision,kind='numbering',limit=1,cursor=entries['next_cursor'])
        assert next_page['objects'][0]['id']=='numbering:2'
        definition=get(plugin,revision,'numbering:2')['direct_format']
        assert definition['number_id']==2 and definition['levels'][0]['format']=='decimal'
        target=get(plugin,revision,'paragraph:2');result=apply_list(plugin,revision,target,2)
        after=get(plugin,result['candidate_revision'],'paragraph:2')
        assert after['text']==target['text']
        assert after['direct_format']['properties']['number_id']==2
        assert after['direct_format']['properties']['list_level']==0
        assert after['direct_format']['runs']==target['direct_format']['runs']
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
        assert apply_list(plugin,revision,target,2)==result
        assert apply_list(plugin,result['candidate_revision'],after,2,key='same')['status']=='unchanged'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_list_removal_suppresses_inherited_style_and_preserves_definition(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        def transform(parts):
            styles=ET.fromstring(parts['word/styles.xml'])
            style=next(s for s in styles if s.get('{'+W+'}styleId')=='Body')
            p=style.find('{'+W+'}pPr');n=ET.Element('{'+W+'}numPr');ET.SubElement(n,'{'+W+'}numId',{'{'+W+'}val':'2'});p.insert(0,n)
            parts['word/styles.xml']=ET.tostring(styles)
        rewrite(tmp_path,original,transform);revision=store.file_hash('report.docx')
        target=get(plugin,revision,'paragraph:2');result=apply_list(plugin,revision,target,0)
        props=get(plugin,result['candidate_revision'],'paragraph:2')['direct_format']['properties']
        assert props['number_id']==0 and 'list_level' not in props and props['style_id']=='Body'
        with zipfile.ZipFile(tmp_path/'report.docx') as old,zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as new:
            assert old.read('word/styles.xml')==new.read('word/styles.xml')
            assert old.read('word/numbering.xml')==new.read('word/numbering.xml')
    finally:store.close()


def test_multilevel_existing_definition_and_start_override(tmp_path):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        def transform(parts):
            root=ET.fromstring(parts['word/numbering.xml']);abstract=next(a for a in root if a.get('{'+W+'}abstractNumId')=='2')
            level=ET.fromstring(ET.tostring(abstract.find('{'+W+'}lvl')));level.set('{'+W+'}ilvl','1');level.find('{'+W+'}lvlText').set('{'+W+'}val','%1.%2.');abstract.append(level)
            number=next(n for n in root if n.get('{'+W+'}numId')=='2');override=ET.SubElement(number,'{'+W+'}lvlOverride',{'{'+W+'}ilvl':'1'});ET.SubElement(override,'{'+W+'}startOverride',{'{'+W+'}val':'4'})
            parts['word/numbering.xml']=ET.tostring(root)
        rewrite(tmp_path,original,transform);revision=store.file_hash('report.docx')
        levels=get(plugin,revision,'numbering:2')['direct_format']['levels'];assert levels[1]['start']==4 and levels[1]['marker']=='%1.%2.'
        result=apply_list(plugin,revision,get(plugin,revision,'paragraph:2'),2,1)
        assert get(plugin,result['candidate_revision'],'paragraph:2')['direct_format']['properties']['list_level']==1
    finally:store.close()


@pytest.mark.parametrize('number,level',[(999,0),(2,8)])
def test_missing_definition_or_level_rejects_without_staging(tmp_path,number,level):
    store,plugin,original,revision=saved_table(tmp_path)
    try:
        with pytest.raises(HarnessError):apply_list(plugin,revision,get(plugin,revision,'paragraph:2'),number,level)
        assert not store.candidate('report.docx') and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.parametrize('args',[{'number_id':-1},{'number_id':2,'level':9},{'number_id':0,'level':1},{'number_id':True}])
def test_list_contract_bounds(args):
    with pytest.raises(ValidationError):SetList(id='list',target='paragraph:1',expected_hash='0'*64,**args)


def restart_list(plugin, revision, target, start=1, number=2, key='restart', **extra):
    op = {'id': 'restart', 'op': 'set_list', 'target': target['id'],
          'expected_hash': target['hash'], 'number_id': number, 'level': 0, 'restart_at': start}
    return plugin.stage_batch(path='report.docx', revision=revision, operations=[op],
                              idempotency_key=key, **extra)


def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return {name: package.read(name) for name in package.namelist()}


@pytest.mark.parametrize('start', [1, 5, 32767])
def test_restart_new_instance_selected_only_continue_preserve_and_undo(tmp_path, start):
    from localoffice.word.contracts import BatchReceipt
    store, plugin, original, revision = saved_table(tmp_path)
    try:
        target = get(plugin, revision, 'paragraph:2')
        result = restart_list(plugin, revision, target, start)
        assert BatchReceipt.model_validate(result).model_dump()['results'][0]['number_id'] == 3
        from localoffice.word.evidence import model_receipt
        assert model_receipt(result)['results'][0]['number_id'] == 3
        assert result['results'][0]['number_id'] == 3
        before = parts(original); after = parts(store.blob(result['candidate_revision']))
        assert before.keys() == after.keys()
        assert {n for n in before if before[n] != after[n]} == {'word/document.xml', 'word/numbering.xml'}
        old = ET.fromstring(before['word/numbering.xml']); new = ET.fromstring(after['word/numbering.xml'])
        assert len(new) == len(old) + 2
        retained = [n for n in new if n.get('{'+W+'}abstractNumId') != '3' and n.get('{'+W+'}numId') != '3']
        assert [ET.tostring(n) for n in retained] == [ET.tostring(n) for n in old]
        source_definition = next(n for n in old if n.get('{'+W+'}abstractNumId') == '2')
        cloned_definition = next(n for n in new if n.get('{'+W+'}abstractNumId') == '3')
        assert cloned_definition.find('{'+W+'}nsid') is not None
        assert [ET.tostring(n) for n in cloned_definition if n.tag != '{'+W+'}nsid'] == [ET.tostring(n) for n in source_definition if n.tag != '{'+W+'}nsid']
        fresh = new[-1]
        assert fresh.get('{'+W+'}numId') == '3'
        assert fresh.find('{'+W+'}abstractNumId').get('{'+W+'}val') == '3'
        assert fresh.find('{'+W+'}lvlOverride').get('{'+W+'}ilvl') == '0'
        assert fresh.find('.//{'+W+'}startOverride').get('{'+W+'}val') == str(start)
        old_doc = ET.fromstring(before['word/document.xml']); new_doc = ET.fromstring(after['word/document.xml'])
        old_ps = old_doc.findall('.//{'+W+'}p'); new_ps = new_doc.findall('.//{'+W+'}p')
        for i, (a,b) in enumerate(zip(old_ps,new_ps)):
            if i != 1: assert ET.tostring(a) == ET.tostring(b)
        current = get(plugin, result['candidate_revision'], 'paragraph:2')
        assert current['text'] == target['text']
        assert current['direct_format']['runs'] == target['direct_format']['runs']
        assert current['direct_format']['properties']['style_id'] == target['direct_format']['properties']['style_id']
        assert restart_list(plugin, revision, target, start) == result
        store.apply(result['receipt_id'], expected_candidate=result['candidate_revision'], action='apply')
        # Later paragraphs join only by explicit continuation, without another allocation.
        later = get(plugin, result['candidate_revision'], 'paragraph:3')
        continued = apply_list(plugin, result['candidate_revision'], later, 3, key='continue')
        assert parts(store.blob(continued['candidate_revision']))['word/numbering.xml'] == after['word/numbering.xml']
        store.apply(continued['receipt_id'], expected_candidate=continued['candidate_revision'], action='apply')
        undo = store.undo(continued['receipt_id'], 'undo-continue')
        store.apply(undo['id'], expected_candidate=undo['candidate_hash'], action='apply')
        assert (tmp_path/'report.docx').read_bytes() == store.blob(result['candidate_revision'])
        # Undo of the original restart itself restores the exact original package.
        undo = store.undo(result['receipt_id'], 'undo-restart')
        store.apply(undo['id'], expected_candidate=undo['candidate_hash'], action='apply')
        assert (tmp_path/'report.docx').read_bytes() == original
    finally: store.close()


def test_restart_preserves_full_level_override_and_field_runs(tmp_path):
    store, plugin, original, revision = saved_table(tmp_path)
    try:
        def transform(p):
            root = ET.fromstring(p['word/numbering.xml'])
            abstract = next(n for n in root if n.get('{'+W+'}abstractNumId') == '2')
            instance = next(n for n in root if n.get('{'+W+'}numId') == '2')
            abstract.insert(0, ET.Element('{'+W+'}nsid', {'{'+W+'}val': '1234ABCD'}))
            override = ET.SubElement(instance, '{'+W+'}lvlOverride', {'{'+W+'}ilvl':'0'})
            ET.SubElement(override, '{'+W+'}startOverride', {'{'+W+'}val':'7'})
            level = ET.fromstring(ET.tostring(abstract.find('{'+W+'}lvl')))
            level.find('{'+W+'}lvlText').set('{'+W+'}val', '%1)')
            override.append(level)
            p['word/numbering.xml'] = ET.tostring(root)
            document = ET.fromstring(p['word/document.xml'])
            target = document.findall('.//{'+W+'}p')[1]
            field = ET.SubElement(target, '{'+W+'}fldSimple', {'{'+W+'}instr':' PAGE '})
            ET.SubElement(ET.SubElement(field, '{'+W+'}r'), '{'+W+'}t').text = '1'
            p['word/document.xml'] = ET.tostring(document)
        rewrite(tmp_path, original, transform)
        source = (tmp_path/'report.docx').read_bytes(); revision = store.file_hash('report.docx')
        result = restart_list(plugin, revision, get(plugin, revision, 'paragraph:2'), 5)
        before = parts(source); after = parts(store.blob(result['candidate_revision']))
        root = ET.fromstring(after['word/numbering.xml'])
        old = next(n for n in root if n.get('{'+W+'}numId') == '2'); fresh = root[-1]
        clone = next(n for n in root if n.get('{'+W+'}abstractNumId') == fresh.find('{'+W+'}abstractNumId').get('{'+W+'}val'))
        assert clone.find('{'+W+'}nsid').get('{'+W+'}val') != '1234ABCD'
        source_root = ET.fromstring(before['word/numbering.xml'])
        for source_node in source_root:
            assert any(ET.tostring(source_node) == ET.tostring(n) for n in root)
        assert ET.tostring(old.find('.//{'+W+'}lvl')) == ET.tostring(fresh.find('.//{'+W+'}lvl'))
        assert old.find('.//{'+W+'}startOverride').get('{'+W+'}val') == '7'
        assert fresh.find('.//{'+W+'}startOverride').get('{'+W+'}val') == '5'
        old_p = ET.fromstring(before['word/document.xml']).findall('.//{'+W+'}p')[1]
        new_p = ET.fromstring(after['word/document.xml']).findall('.//{'+W+'}p')[1]
        assert [ET.tostring(n) for n in old_p if n.tag != '{'+W+'}pPr'] == [ET.tostring(n) for n in new_p if n.tag != '{'+W+'}pPr']
    finally: store.close()


@pytest.mark.parametrize('fault', ['multilevel','style_link','override_level1','override_bullet','override_restart','override_link','undefined','tracked'])
def test_restart_rejects_unsupported_semantics_without_staging(tmp_path, fault):
    store, plugin, original, revision = saved_table(tmp_path)
    try:
        def transform(p):
            root = ET.fromstring(p['word/numbering.xml'])
            abstract = next(n for n in root if n.get('{'+W+'}abstractNumId') == '2')
            instance = next(n for n in root if n.get('{'+W+'}numId') == '2')
            if fault == 'multilevel':
                level=ET.fromstring(ET.tostring(abstract.find('{'+W+'}lvl')));level.set('{'+W+'}ilvl','1');abstract.append(level)
            elif fault == 'style_link':ET.SubElement(abstract,'{'+W+'}numStyleLink',{'{'+W+'}val':'Normal'})
            elif fault == 'undefined':instance.find('{'+W+'}abstractNumId').set('{'+W+'}val','999')
            elif fault.startswith('override_'):
                override=ET.SubElement(instance,'{'+W+'}lvlOverride',{'{'+W+'}ilvl':'1' if fault=='override_level1' else '0'})
                level=ET.fromstring(ET.tostring(abstract.find('{'+W+'}lvl')));override.append(level)
                if fault=='override_bullet':level.find('{'+W+'}numFmt').set('{'+W+'}val','bullet')
                if fault=='override_restart':ET.SubElement(level,'{'+W+'}lvlRestart',{'{'+W+'}val':'0'})
                if fault=='override_link':ET.SubElement(level,'{'+W+'}pStyle',{'{'+W+'}val':'Normal'})
            else:
                doc=ET.fromstring(p['word/document.xml']);target=doc.findall('.//{'+W+'}p')[1]
                ET.SubElement(target.find('{'+W+'}pPr'),'{'+W+'}pPrChange',{'{'+W+'}id':'1','{'+W+'}author':'test'})
                p['word/document.xml']=ET.tostring(doc)
            p['word/numbering.xml']=ET.tostring(root)
        rewrite(tmp_path, original, transform);source=(tmp_path/'report.docx').read_bytes();revision=store.file_hash('report.docx')
        with pytest.raises(HarnessError):restart_list(plugin,revision,get(plugin,revision,'paragraph:2'))
        assert not store.candidate('report.docx') and (tmp_path/'report.docx').read_bytes()==source
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1 # fixture creation only
    finally:store.close()


@pytest.mark.parametrize('patch',[{'restart_at':0},{'restart_at':32768},{'restart_at':True}, {'restart_at':1,'number_id':0},{'restart_at':1,'level':1}])
def test_restart_contract_bounds(patch):
    with pytest.raises(ValidationError):SetList(id='restart',target='paragraph:1',expected_hash='0'*64,**{'number_id':2,**patch})


@pytest.mark.asyncio
async def test_restart_render_isolates_original_list_after_explicit_continuation(tmp_path):
    from pypdf import PdfReader
    from localoffice.word.render import WordRenderer
    store, plugin, original, revision = saved_table(tmp_path)
    try:
        first = restart_list(plugin, revision, get(plugin, revision, 'paragraph:2'), 7)
        second = apply_list(plugin, first['candidate_revision'],
                             get(plugin, first['candidate_revision'], 'paragraph:3'),
                             first['results'][0]['number_id'], key='continue-render')
        rendered = await WordRenderer(store).render(path='report.docx', revision=second['candidate_revision'])
        pdf = PdfReader(io.BytesIO(store.blob(rendered['pdf_hash'])))
        assert len(pdf.pages) == 2
        text = ' '.join(pdf.pages[0].extract_text().split())
        assert '7. Evidence-backed introduction.' in text, text
        assert '8. First item' in text, text
        assert '1. Numbered item' in text, text
        assert 'Next page' in pdf.pages[1].extract_text()
        assert (tmp_path/'report.docx').read_bytes() == original
    finally:
        store.close()
