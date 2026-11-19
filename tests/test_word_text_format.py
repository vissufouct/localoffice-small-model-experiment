import io,zipfile
from xml.etree import ElementTree as ET
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import FormatText
from test_word_tables import saved_table
from test_word_lists import rewrite
from test_word_creation import W

N='{'+W+'}'


def fixture(root):
    store,plugin,original,rev=saved_table(root)
    def change(parts):
        doc=ET.fromstring(parts['word/document.xml']);p=doc.find(N+'body').findall(N+'p')[1]
        for e in list(p):
            if e.tag!=N+'pPr':p.remove(e)
        run=ET.SubElement(p,N+'r',{N+'rsidR':'00112233'});props=ET.SubElement(run,N+'rPr');ET.SubElement(props,N+'i')
        ET.SubElement(run,N+'t',{'{http://www.w3.org/XML/1998/namespace}space':'preserve'}).text='A😀 bold '
        ET.SubElement(p,N+'bookmarkStart',{N+'id':'1',N+'name':'mark'})
        link=ET.SubElement(p,N+'hyperlink',{N+'anchor':'mark'});run=ET.SubElement(link,N+'r');props=ET.SubElement(run,N+'rPr');ET.SubElement(props,N+'color',{N+'val':'008800'});ET.SubElement(run,N+'t').text='linked'
        ET.SubElement(p,N+'bookmarkEnd',{N+'id':'1'})
        run=ET.SubElement(p,N+'r');ET.SubElement(run,N+'tab');ET.SubElement(run,N+'t').text='Z';ET.SubElement(run,N+'br');ET.SubElement(run,N+'t').text='end'
        parts['word/document.xml']=ET.tostring(doc)
    rewrite(root,original,change)
    return store,plugin,(root/'report.docx').read_bytes(),store.file_hash('report.docx')


def get(plugin,rev):return plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]


def edit(plugin,rev,target,start,end,properties,key='format'):
    return plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'format_text','target':target['id'],'expected_hash':target['hash'],'start':start,'end':end,'properties':properties}],idempotency_key=key)


def characters(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:p=ET.fromstring(z.read('word/document.xml')).find(N+'body').findall(N+'p')[1]
    result=[]
    for run in p.iter(N+'r'):
        props=run.find(N+'rPr');values={e.tag:dict(e.attrib) for e in props} if props is not None else {}
        for e in run:
            text=e.text or '' if e.tag==N+'t' else '\t' if e.tag==N+'tab' else '\n' if e.tag==N+'br' else ''
            result.extend((ch,dict(values),dict(run.attrib)) for ch in text)
    return p,result


def test_unicode_cross_run_hyperlink_span_preserves_unselected_content_noop_and_undo(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        target=get(plugin,rev);assert target['text']=='A😀 bold linked\tZ\nend'
        result=edit(plugin,rev,target,3,12,{'bold':True,'color':'17365D'})
        data=store.blob(result['candidate_revision']);a,before=characters(original);b,after=characters(data)
        assert get(plugin,result['candidate_revision'])['text']==target['text']
        assert len(before)==len(after)
        for i,(old,new) in enumerate(zip(before,after)):
            assert old[0]==new[0] and old[2]==new[2]
            expected={**old[1],N+'b':{N+'val':'true'},N+'bCs':{N+'val':'true'},N+'color':{N+'val':'17365D'}} if 3<=i<12 else old[1]
            assert new[1]==expected
        assert a.find(N+'hyperlink').attrib==b.find(N+'hyperlink').attrib
        for name in ['pPr','bookmarkStart','bookmarkEnd']:
            assert ET.canonicalize(ET.tostring(a.find(N+name)))==ET.canonicalize(ET.tostring(b.find(N+name)))
        with zipfile.ZipFile(io.BytesIO(original)) as old,zipfile.ZipFile(io.BytesIO(data)) as new:
            for name in old.namelist():
                if name!='word/document.xml':assert old.read(name)==new.read(name)
            olddoc=ET.fromstring(old.read('word/document.xml'));newdoc=ET.fromstring(new.read('word/document.xml'))
            for doc in (olddoc,newdoc):
                body=doc.find(N+'body');body.remove(body.findall(N+'p')[1])
            assert ET.canonicalize(ET.tostring(olddoc))==ET.canonicalize(ET.tostring(newdoc))
        assert edit(plugin,rev,target,3,12,{'bold':True,'color':'17365D'})==result
        current=get(plugin,result['candidate_revision'])
        assert edit(plugin,result['candidate_revision'],current,3,12,{'bold':True,'color':'17365D'},'noop')['status']=='unchanged'
        assert (tmp_path/'report.docx').read_bytes()==original
        store.apply(result['receipt_id'],expected_candidate=result['candidate_revision'],action='apply')
        undo=store.undo(result['receipt_id'],'undo');store.apply(undo['id'],expected_candidate=undo['candidate_hash'],action='apply')
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_span_offsets_include_tabs_breaks_and_astral_codepoints(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        target=get(plugin,rev);result=edit(plugin,rev,target,1,2,{'size_points':16})
        before=characters(original)[1];after=characters(store.blob(result['candidate_revision']))[1]
        assert after[1][0]=='😀' and after[1][1][N+'sz']=={N+'val':'32'}
        assert before[:1]==after[:1] and before[2:]==after[2:]
        rev=result['candidate_revision'];target=get(plugin,rev)
        result=edit(plugin,rev,target,14,17,{'bold':True},'breaks')
        assert get(plugin,result['candidate_revision'])['text']==target['text']
        chars=characters(store.blob(result['candidate_revision']))[1]
        assert ''.join(x[0] for x in chars[14:17])=='\tZ\n'
        assert all(N+'b' in x[1] for x in chars[14:17])
    finally:store.close()


def test_format_span_rejects_invalid_ranges_layout_fields_and_complex_content(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        target=get(plugin,rev)
        for start,end in [(0,0),(3,2),(-1,2)]:
            with pytest.raises(ValidationError):FormatText(target=target['id'],expected_hash=target['hash'],start=start,end=end,properties={'bold':True})
        with pytest.raises(ValidationError):FormatText(target=target['id'],expected_hash=target['hash'],start=0,end=1,properties={'alignment':'center'})
        with pytest.raises(HarnessError,match='Invalid nonempty'):edit(plugin,rev,target,0,999,{'bold':True})
        with pytest.raises(HarnessError,match='stale_object'):edit(plugin,rev,{**target,'hash':'0'*64},0,1,{'bold':True})
        def field(parts):
            doc=ET.fromstring(parts['word/document.xml']);run=doc.find(N+'body').findall(N+'p')[1].find(N+'r');ET.SubElement(run,N+'fldChar',{N+'fldCharType':'begin'});parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,field);rev=store.file_hash('report.docx')
        with pytest.raises(HarnessError,match='Unsupported run content'):edit(plugin,rev,get(plugin,rev),0,1,{'bold':True})
        assert store.candidate('report.docx') is None
    finally:store.close()
