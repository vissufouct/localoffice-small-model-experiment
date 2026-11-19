import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.storage import ProjectStore,digest
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer
from test_word_page_bands import band_design
from test_word_field_caches import parts,NS,render_cached


def archive(values):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        for name,data in values.items():z.writestr(name,data)
    return stream.getvalue()


def source(store,plugin,root,toc=False,body=False,locked=False):
    args=band_design()
    if toc:args['blocks']=[{'kind':'table_of_contents'},{'kind':'page_break'},*args['blocks']]
    created=plugin.create(**args,idempotency_key='fixture')
    store.apply(created['receipt_id'],expected_candidate=created['candidate_revision'],action='save')
    data=(root/'report.docx').read_bytes()
    if body or locked:
        values=parts(data)
        if body:
            doc=ET.fromstring(values['word/document.xml']);last=doc.find(NS+'body').findall(NS+'p')[-1]
            field=ET.SubElement(last,NS+'fldSimple',{NS+'instr':' PAGE ','{'+NS[1:-1]+'}dirty':'true'})
            ET.SubElement(ET.SubElement(field,NS+'r'),NS+'t').text='1'
            values['word/document.xml']=ET.tostring(doc)
        if locked:
            footer=ET.fromstring(values['word/footer1.xml']);next(footer.iter(NS+'fldSimple')).set(NS+'fldLock','true')
            values['word/footer1.xml']=ET.tostring(footer)
        data=archive(values);(root/'report.docx').write_bytes(data)
    return data,digest(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('toc',[False,True])
async def test_page_cache_refresh_preserves_fields_formatting_other_parts_and_undo(tmp_path,toc):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        original,rev=source(store,plugin,tmp_path,toc=toc)
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='refresh')
        assert (tmp_path/'report.docx').read_bytes()==original
        assert await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='refresh')==result
        before=parts(original);after=parts(store.blob(result['candidate_revision']))
        allowed={'word/footer1.xml'}|({'word/document.xml','word/styles.xml'} if toc else set())
        assert before.keys()==after.keys()
        for name in before:
            if name not in allowed:assert before[name]==after[name],name
        old=ET.fromstring(before['word/footer1.xml']);new=ET.fromstring(after['word/footer1.xml'])
        oldfields=list(old.iter(NS+'fldSimple'));newfields=list(new.iter(NS+'fldSimple'))
        expected=str(3 if toc else 2)
        assert [''.join(f.itertext()) for f in newfields]==[expected,expected]
        assert [f.get(NS+'instr') for f in newfields]==[' PAGE ',' NUMPAGES ']
        for a,b in zip(oldfields,newfields):
            # Only numeric cached text and the dirty flag are allowed to differ.
            b.set(NS+'dirty',a.get(NS+'dirty'))
            for oldtext,newtext in zip(a.iter(NS+'t'),b.iter(NS+'t')):newtext.text=oldtext.text
        assert ET.tostring(old)==ET.tostring(new)
        pdf=await render_cached(store.blob(result['candidate_revision']),tmp_path/'raw-cache')
        assert len(pdf.pages)==int(expected)
        for n,page in enumerate(pdf.pages,1):assert f'Page {n} of {expected}' in ' '.join(page.extract_text().split())
        again=await plugin.call_async('refresh_fields',path='report.docx',revision=result['candidate_revision'],idempotency_key='noop')
        assert again['status']=='unchanged'
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.asyncio
async def test_body_page_field_updates_on_its_actual_page_with_toc(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        original,rev=source(store,plugin,tmp_path,toc=True,body=True)
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='refresh')
        doc=ET.fromstring(parts(store.blob(result['candidate_revision']))['word/document.xml'])
        field=list(doc.iter(NS+'fldSimple'))[-1]
        assert ''.join(field.itertext())=='3'
        assert field.get(NS+'instr')==' PAGE '
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


@pytest.mark.asyncio
async def test_locked_page_field_is_not_unlocked_or_partially_updated(tmp_path):
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        original,rev=source(store,plugin,tmp_path,locked=True)
        with pytest.raises(HarnessError,match='Locked'):
            await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='refresh')
        assert not store.candidate('report.docx') and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()

@pytest.mark.asyncio
async def test_split_complex_field_codes_and_roman_results_keep_run_formatting(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        data,rev=source(store,plugin,tmp_path);values=parts(data)
        footer=ET.fromstring(values['word/footer1.xml']);p=footer.find(NS+'p')
        for simple in list(p.findall(NS+'fldSimple')):
            code=simple.get(NS+'instr').strip()+' \\* ROMAN'
            nodes=[]
            def run(child):
                r=ET.Element(NS+'r');r.append(child);nodes.append(r)
            run(ET.Element(NS+'fldChar',{NS+'fldCharType':'begin',NS+'dirty':'true'}))
            for part in [code[:2],code[2:]]:
                text=ET.Element(NS+'instrText');text.text=part;run(text)
            run(ET.Element(NS+'fldChar',{NS+'fldCharType':'separate'}))
            result=ET.Element(NS+'r');props=ET.SubElement(result,NS+'rPr');ET.SubElement(props,NS+'i');ET.SubElement(props,NS+'color',{NS+'val':'17365D'})
            ET.SubElement(result,NS+'t').text='I';nodes.append(result)
            run(ET.Element(NS+'fldChar',{NS+'fldCharType':'end'}))
            position=list(p).index(simple);p.remove(simple)
            for offset,node in enumerate(nodes):p.insert(position+offset,node)
        values['word/footer1.xml']=ET.tostring(footer);original=archive(values);(tmp_path/'report.docx').write_bytes(original)
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=digest(original),idempotency_key='roman')
        updated=ET.fromstring(parts(store.blob(result['candidate_revision']))['word/footer1.xml'])
        numbers=[n for n in updated.iter(NS+'t') if n.text=='II'];assert len(numbers)==2
        assert [n.text for n in footer.iter(NS+'instrText')]==[n.text for n in updated.iter(NS+'instrText')]
        for node in updated.iter(NS+'fldChar'):
            if node.get(NS+'fldCharType')=='begin':node.set(NS+'dirty','true')
        for node in numbers:node.text='I'
        assert ET.tostring(updated)==ET.tostring(footer)
    finally:store.close()


@pytest.mark.asyncio
async def test_default_first_even_footer_caches_follow_section_relationships(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        data,rev=source(store,plugin,tmp_path,toc=True);values=parts(data)
        doc=ET.fromstring(values['word/document.xml']);section=doc.find('.//'+NS+'sectPr')
        relns='http://schemas.openxmlformats.org/package/2006/relationships'
        from xml.dom import minidom
        links=minidom.parseString(values['word/_rels/document.xml.rels'])
        types=minidom.parseString(values['[Content_Types].xml'])
        rns='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
        for kind,part in [('first','first-footer.xml'),('even','even-footer.xml')]:
            footer=ET.fromstring(values['word/footer1.xml']);next(footer.iter(NS+'t')).text=kind+' Page '
            values['word/'+part]=ET.tostring(footer)
            for metadata,tag,attrs in [(links,'Relationship',{'Id':'test-'+kind,'Type':rns[1:-1]+'/footer','Target':part}),
                    (types,'Override',{'PartName':'/word/'+part,'ContentType':'application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml'})]:
                node=metadata.createElementNS(metadata.documentElement.namespaceURI,tag)
                for key,value in attrs.items():node.setAttribute(key,value)
                metadata.documentElement.appendChild(node)
            position=max(i for i,n in enumerate(section) if n.tag in [NS+'headerReference',NS+'footerReference'])+1
            section.insert(position,ET.Element(NS+'footerReference',{NS+'type':kind,rns+'id':'test-'+kind}))
        # sectPr titlePg follows page size/margins; generated source ends there.
        section.append(ET.Element(NS+'titlePg'))
        settings=ET.fromstring(values['word/settings.xml']);settings.insert(0,ET.Element(NS+'evenAndOddHeaders'))
        values.update({'word/document.xml':ET.tostring(doc),'word/_rels/document.xml.rels':links.toxml(encoding='utf-8'),
                       '[Content_Types].xml':types.toxml(encoding='utf-8'),'word/settings.xml':ET.tostring(settings)})
        original=archive(values);(tmp_path/'report.docx').write_bytes(original)
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=digest(original),idempotency_key='variants')
        result_parts=parts(store.blob(result['candidate_revision']))
        for part,expected in [('first-footer.xml','1'),('even-footer.xml','2'),('footer1.xml','3')]:
            fields=list(ET.fromstring(result_parts['word/'+part]).iter(NS+'fldSimple'))
            assert [''.join(f.itertext()) for f in fields]==[expected,'3']
        for name in ['word/_rels/document.xml.rels','[Content_Types].xml','word/settings.xml','word/header1.xml']:
            assert result_parts[name]==values[name]
        pdf=await render_cached(store.blob(result['candidate_revision']),tmp_path/'variants-render')
        assert 'first Page 1 of 3' in ' '.join(pdf.pages[0].extract_text().split())
        assert 'even Page 2 of 3' in ' '.join(pdf.pages[1].extract_text().split())
        assert 'Page 3 of 3' in ' '.join(pdf.pages[2].extract_text().split())
    finally:store.close()

@pytest.mark.asyncio
async def test_page_number_restart_is_not_confused_with_total_page_count(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        data,rev=source(store,plugin,tmp_path);values=parts(data)
        doc=ET.fromstring(values['word/document.xml']);doc.find('.//'+NS+'sectPr').append(ET.Element(NS+'pgNumType',{NS+'start':'5'}))
        values['word/document.xml']=ET.tostring(doc);original=archive(values);(tmp_path/'report.docx').write_bytes(original)
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=digest(original),idempotency_key='restart')
        candidate=parts(store.blob(result['candidate_revision']))
        fields=list(ET.fromstring(candidate['word/footer1.xml']).iter(NS+'fldSimple'))
        assert [''.join(f.itertext()) for f in fields]==['6','2']
        assert candidate['word/document.xml']==values['word/document.xml']
        pdf=await render_cached(store.blob(result['candidate_revision']),tmp_path/'restart-render')
        for n,page in enumerate(pdf.pages,5):assert f'Page {n} of 2' in ' '.join(page.extract_text().split())
    finally:store.close()


@pytest.mark.asyncio
async def test_page_only_refresh_refuses_changed_body_in_pagination_snapshot(tmp_path):
    from localoffice.word.contracts import RefreshFields
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        data,rev=source(store,plugin,tmp_path)
        rendered=await WordRenderer(store).render(path='report.docx',revision=rev,capture_fields=True)
        snapshot=parts(store.blob(rendered['field_snapshot_hash']))
        doc=ET.fromstring(snapshot['word/document.xml']);next(doc.iter(NS+'t')).text='Changed body'
        snapshot['word/document.xml']=ET.tostring(doc)
        with pytest.raises(HarnessError,match='body content'):
            plugin._execute(RefreshFields(path='report.docx',revision=rev),'refresh_fields',field_snapshot=archive(snapshot))
        assert not store.candidate('report.docx') and (tmp_path/'report.docx').read_bytes()==data
    finally:store.close()
