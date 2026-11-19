import copy,json
import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import FindText
from localoffice.runtime import WORD_ACTION,TaskRunner
from localoffice.word.guidance import project_schema
from test_word_tables import saved_table
from test_word_text_format import fixture


def test_search_returns_exact_target_offsets_and_table_content_without_mutation(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        result=plugin.find_text(path='report.docx',revision=rev,query='Evidence-backed')
        assert len(result['matches'])==1 and result['next_cursor'] is None
        m=result['matches'][0];assert m['id']=='paragraph:2' and (m['start'],m['end'])==(0,15)
        read=plugin.read_objects(path='report.docx',revision=rev,ids=[m['id']])['objects'][0]
        assert m['hash']==read['hash'] and m['text']==read['text']
        assert plugin.find_text(path='report.docx',revision=rev,query='Cedar')['matches'][0]['text']=='Cedar'
        assert not plugin.find_text(path='report.docx',revision=rev,query='evidence-backed')['matches']
        assert plugin.find_text(path='report.docx',revision=rev,query='evidence-backed',case_sensitive=False)['matches'][0]['start']==0
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
    finally:store.close()


def test_search_unicode_offsets_and_revision_query_bound_pagination(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        m=plugin.find_text(path='report.docx',revision=rev,query='😀 bold')['matches'][0]
        assert (m['start'],m['end'])==(1,7)
        assert m['text'][m['start']-m['excerpt_offset']:m['end']-m['excerpt_offset']]=='😀 bold'
        result=plugin.find_text(path='report.docx',revision=rev,query='e',limit=1);seen=[]
        while True:
            seen.extend((m['id'],m['start']) for m in result['matches'])
            cursor=result['next_cursor']
            if not cursor:break
            with pytest.raises(HarnessError,match='stale_search_cursor'):
                plugin.find_text(path='report.docx',revision=rev,query='a',cursor=cursor)
            result=plugin.find_text(path='report.docx',revision=rev,query='e',cursor=cursor,limit=1)
        assert len(seen)>1 and len(set(seen))==len(seen)
        cursor=plugin.find_text(path='report.docx',revision=rev,query='e',limit=1)['next_cursor']
        target=plugin.read_objects(path='report.docx',revision=rev,ids=['paragraph:2'])['objects'][0]
        changed=plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'format_text','target':target['id'],'expected_hash':target['hash'],'start':0,'end':1,'properties':{'bold':True}}],idempotency_key='change')
        with pytest.raises(HarnessError,match='stale_search_cursor'):
            plugin.find_text(path='report.docx',revision=changed['candidate_revision'],query='e',cursor=cursor)
    finally:store.close()


@pytest.mark.parametrize('props',[{'query':''},{'query':'a'*201},{'query':'\x00'},{'query':'\ud800'},{'query':'x','limit':9}])
def test_search_contract_bounds(props):
    with pytest.raises(ValidationError):FindText(path='a.docx',revision='a'*64,**props)


@pytest.mark.asyncio
async def test_search_runtime_schema_registry_and_graph_are_available_without_loading_edits(tmp_path):
    store,plugin,original,rev=saved_table(tmp_path)
    class Provider:
        context_limit=4096
        def __init__(self):self.step=0
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 500
        async def stream(self,messages,**kwargs):
            self.step+=1
            assert 'FindText' in kwargs['schema']['$defs'] and 'StageBatch' not in kwargs['schema']['$defs']
            action={'tool':'word.find_text','path':'report.docx','revision':rev,'query':'Evidence-backed'} if self.step==1 else {'tool':'finish','text':'Located the introduction; no edits requested.'}
            yield {'type':'token','text':json.dumps(action)}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider())
    try:
        task=await runner.start('Find Evidence-backed without editing.',artifact='report.docx');await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()[0]=='completed'
        result=next(e['result'] for e in runner.events if e['type']=='tool' and e['tool']=='word.find_text')
        assert result['matches'][0]['id']=='paragraph:2'
        assert store.db.execute("SELECT 1 FROM edges WHERE source=? AND relation='read-object'",('task:'+task,)).fetchone()
        assert (tmp_path/'report.docx').read_bytes()==original
    finally:await runner.pause();store.close()


@pytest.mark.parametrize('text,query,starts',[('aaa','aa',[0,1]),('x'*3000+'needle'+'z'*3000,'needle',[3000])])
def test_search_overlaps_and_long_paragraph_excerpts_are_bounded(tmp_path,text,query,starts):
    from test_word_lists import rewrite
    from test_word_creation import W
    from xml.etree import ElementTree as ET
    store,plugin,original,rev=saved_table(tmp_path)
    try:
        def replace(parts):
            doc=ET.fromstring(parts['word/document.xml']);p=doc.find('{'+W+'}body').findall('{'+W+'}p')[1]
            for e in list(p):p.remove(e)
            run=ET.SubElement(p,'{'+W+'}r');ET.SubElement(run,'{'+W+'}t').text=text
            parts['word/document.xml']=ET.tostring(doc)
        rewrite(tmp_path,original,replace);rev=store.file_hash('report.docx')
        result=plugin.find_text(path='report.docx',revision=rev,query=query)
        assert [m['start'] for m in result['matches']]==starts
        for m in result['matches']:
            assert len(m['text'])<=280
            assert m['text'][m['start']-m['excerpt_offset']:m['end']-m['excerpt_offset']]==query
        assert len(json.dumps(result))<2500
    finally:store.close()
