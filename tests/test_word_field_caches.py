import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.plugin import WordPlugin
from localoffice.word.render import WordRenderer
from localoffice.word.contracts import BatchReceipt
from test_word_contents import toc_design
from test_word_creation import W

NS='{'+W+'}'
def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:return {name:z.read(name) for name in z.namelist()}

@pytest.mark.asyncio
async def test_toc_cache_transfer_preserves_body_parts_links_and_exact_undo(tmp_path):
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        created=plugin.create(**toc_design(),idempotency_key='create');rev=created['candidate_revision']
        store.apply(created['receipt_id'],expected_candidate=rev,action='save');original=(tmp_path/'report.docx').read_bytes()
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='refresh')
        assert BatchReceipt(**result).warnings
        assert await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='refresh')==result
        assert (tmp_path/'report.docx').read_bytes()==original
        candidate=store.blob(result['candidate_revision']);before=parts(original);after=parts(candidate)
        assert before.keys()==after.keys()
        for name in before:
            if name not in ['word/document.xml','word/styles.xml']:assert before[name]==after[name],name
        source=ET.fromstring(before['word/document.xml']);updated=ET.fromstring(after['word/document.xml'])
        original_body=source.find(NS+'body');new_body=updated.find(NS+'body')
        assert new_body[0].tag==NS+'sdt'
        assert 'Original report design' in ''.join(new_body[0].itertext())
        assert 'requires a field update' not in ''.join(new_body[0].itertext())
        anchors={b.get(NS+'name') for b in updated.iter(NS+'bookmarkStart')}
        links=[h.get(NS+'anchor') for h in new_body[0].iter(NS+'hyperlink')]
        assert links and all(anchor in anchors for anchor in links)
        for node in new_body:
            for p in node.iter(NS+'p'):
                for mark in list(p):
                    if mark.tag in [NS+'bookmarkStart',NS+'bookmarkEnd']:p.remove(mark)
        assert [ET.tostring(n) for n in list(original_body)[1:]]==[ET.tostring(n) for n in list(new_body)[1:]]
        oldstyles=ET.fromstring(before['word/styles.xml']);newstyles=ET.fromstring(after['word/styles.xml'])
        lookup={s.get(NS+'styleId'):ET.tostring(s) for s in newstyles}
        assert all(lookup[s.get(NS+'styleId')]==ET.tostring(s) for s in oldstyles)
        rendered=await WordRenderer(store).render(path='report.docx',revision=result['candidate_revision'])
        from pypdf import PdfReader
        pdf=PdfReader(io.BytesIO(store.blob(rendered['pdf_hash'])))
        assert len(pdf.pages)==3 and pdf.pages[0].extract_text().strip().endswith('2')
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()

async def render_cached(data,root):
    """Independent verification: plain conversion MUST NOT refresh TOC fields."""
    import asyncio,os,signal,shutil
    from pypdf import PdfReader
    root.mkdir();(root/'cached.docx').write_bytes(data)
    process=await asyncio.create_subprocess_exec(shutil.which('soffice'),'-env:UserInstallation='+(root/'profile').as_uri(),
        '--headless','--nologo','--nodefault','--norestore','--convert-to','pdf:writer_pdf_Export',
        '--outdir',str(root),str(root/'cached.docx'),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
    try:await asyncio.wait_for(process.wait(),60)
    finally:
        if process.returncode is None:os.killpg(process.pid,signal.SIGKILL);await process.wait()
    assert process.returncode==0
    return PdfReader(root/'cached.pdf')

@pytest.mark.asyncio
async def test_saved_multilevel_toc_cache_can_be_read_without_any_field_update(tmp_path):
    from test_word_contents import long_toc_design
    import re
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        args=long_toc_design();created=plugin.create(**args,idempotency_key='create')
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=created['candidate_revision'],idempotency_key='cache')
        pdf=await render_cached(store.blob(result['candidate_revision']),tmp_path/'raw-render')
        texts=[p.extract_text().strip() for p in pdf.pages];first=texts.index('Chapter 01');assert first>=2
        toc='\n'.join(texts[:first])
        for i in range(1,19):
            title=f'Chapter {i:02d}';number=texts.index(title)+1
            assert re.search(re.escape(title)+r'[.\s]*'+str(number)+r'\b',toc),(title,number,toc)
        # Generated point bookmarks must not prevent a later precise heading edit.
        match=plugin.find_text(path='report.docx',revision=result['candidate_revision'],query='Chapter 01')['matches'][-1]
        edited=plugin.replace_text(path='report.docx',revision=result['candidate_revision'],target=match['id'],expected_hash=match['hash'],start=match['start'],end=match['end'],text='Revised 01',idempotency_key='heading')
        refreshed=await plugin.call_async('refresh_fields',path='report.docx',revision=edited['candidate_revision'],idempotency_key='recache')
        actual=ET.fromstring(parts(store.blob(refreshed['candidate_revision']))['word/document.xml'])
        toc=actual.find(NS+'body')[0]
        assert 'Revised 01' in ''.join(toc.itertext()) and 'Chapter 01' not in ''.join(toc.itertext())
        bookmarks=list(actual.iter(NS+'bookmarkStart'))
        assert len(bookmarks)==18 and len({b.get(NS+'name') for b in bookmarks})==18
    finally:store.close()

@pytest.mark.asyncio
async def test_refresh_scope_noop_and_durable_progress(tmp_path):
    from test_word_table_annotations import bind
    from localoffice.errors import HarnessError
    from localoffice.word.progress import completed_changes
    from localoffice.chats import current_chat
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        created=plugin.create(**toc_design(),idempotency_key='create');rev=created['candidate_revision']
        bind(store,plugin,rev,['paragraph:2'])
        with pytest.raises(HarnessError,match='outside'):
            await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='scope',task_id='table-task')
        assert store.candidate('report.docx')['candidate_hash']==rev
        with store.transaction():store.db.execute("UPDATE task_scopes SET ranges='null' WHERE task_id='table-task'")
        result=await plugin.call_async('refresh_fields',path='report.docx',revision=rev,idempotency_key='scope',task_id='table-task')
        progress=completed_changes(store,'table-task',current_chat(store),'report.docx',result['candidate_revision'])
        assert progress['receipts'][0]['changes'][0]['op']=='refresh_fields'
        count=store.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        repeated=await plugin.call_async('refresh_fields',path='report.docx',revision=result['candidate_revision'],idempotency_key='noop',task_id='table-task')
        assert repeated['status']=='unchanged'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==count
    finally:store.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['body','options','destination'])
async def test_cache_transfer_refuses_changed_or_unresolvable_renderer_evidence(tmp_path,damage):
    from localoffice.word.contracts import RefreshFields
    from localoffice.errors import HarnessError
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        result=plugin.create(**toc_design(),idempotency_key='create');rev=result['candidate_revision']
        rendered=await WordRenderer(store).render(path='report.docx',revision=rev)
        snapshot=parts(store.blob(rendered['field_snapshot_hash']));document=ET.fromstring(snapshot['word/document.xml'])
        if damage=='body':
            paragraph=document.find(NS+'body').find(NS+'p');paragraph.find('.//'+NS+'t').text='Modified source'
        elif damage=='options':next(document.iter(NS+'instrText')).text=' TOC \\o "1-6" '
        else:next(document.iter(NS+'hyperlink')).set(NS+'anchor','missing-destination')
        snapshot['word/document.xml']=ET.tostring(document)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            for name,data in snapshot.items():z.writestr(name,data)
        with pytest.raises(HarnessError):plugin._execute(RefreshFields(path='report.docx',revision=rev),'refresh_fields',field_snapshot=output.getvalue())
        assert store.candidate('report.docx')['candidate_hash']==rev
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
    finally:store.close()

@pytest.mark.asyncio
async def test_refresh_does_not_discard_bookmarks_inside_toc(tmp_path):
    from localoffice.word.contracts import RefreshFields
    from localoffice.errors import HarnessError
    from localoffice.storage import digest
    store=ProjectStore(tmp_path);plugin=WordPlugin(store)
    try:
        created=plugin.create(**toc_design(),idempotency_key='create');rev=created['candidate_revision']
        rendered=await WordRenderer(store).render(path='report.docx',revision=rev)
        snapshot=store.blob(rendered['field_snapshot_hash'])
        store.apply(created['receipt_id'],expected_candidate=rev,action='save')
        source=parts((tmp_path/'report.docx').read_bytes());doc=ET.fromstring(source['word/document.xml']);toc=doc.find(NS+'body')[0]
        toc.insert(1,ET.Element(NS+'bookmarkStart',{NS+'id':'9',NS+'name':'UserTocMark'}))
        toc.append(ET.Element(NS+'bookmarkEnd',{NS+'id':'9'}))
        source['word/document.xml']=ET.tostring(doc);buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            for name,value in source.items():z.writestr(name,value)
        data=buf.getvalue();(tmp_path/'report.docx').write_bytes(data)
        with pytest.raises(HarnessError,match='annotations'):
            plugin._execute(RefreshFields(path='report.docx',revision=digest(data)),'refresh_fields',field_snapshot=snapshot)
        assert (tmp_path/'report.docx').read_bytes()==data and not store.candidate('report.docx')
    finally:store.close()
