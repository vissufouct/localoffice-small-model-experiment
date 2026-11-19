import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.word.contracts import FindText
from test_word_story_reads import fixture


def test_explicit_story_scopes_return_editable_targets_and_keep_default_main_only(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        assert not plugin.find_text(path='report.docx',revision=rev,query='Quarterly')['matches']
        header=plugin.find_text(path='report.docx',revision=rev,query='Quarterly',scope='headers')
        assert header['search_scope']=='headers' and len(header['matches'])==1
        m=header['matches'][0];assert (m['start'],m['end'])==(0,9)
        assert m['id']=='paragraph:header:1:1' and m['story']['root_id']=='header:1'
        assert not m['story']['read_only'] and 'replace_text' in m['story']['editable_operations']
        assert not plugin.find_text(path='report.docx',revision=rev,query='Quarterly',scope='footers')['matches']
        footer=plugin.find_text(path='report.docx',revision=rev,query='Page',scope='footers')['matches'][0]
        assert footer['id']=='paragraph:footer:1:1' and footer['story']['contains_fields']
        all_matches=plugin.find_text(path='report.docx',revision=rev,query='report',scope='all')['matches']
        assert {match['id'] for match in all_matches}=={'paragraph:1','paragraph:header:1:1'}
        assert 'story' not in next(match for match in all_matches if match['id']=='paragraph:1')
        assert store.candidate('report.docx') is None and (tmp_path/'report.docx').read_bytes()==original
        result=plugin.stage_batch(path='report.docx',revision=rev,operations=[{'op':'replace_text','target':m['id'],'expected_hash':m['hash'],'start':m['start'],'end':m['end'],'text':'Annual'}],idempotency_key='searched-header')
        read=plugin.read_objects(path='report.docx',revision=result['candidate_revision'],ids=[m['id']])['objects'][0]
        assert read['text']=='Annual report'
    finally:store.close()


def test_story_search_pagination_is_scope_bound(tmp_path):
    store,plugin,original,rev=fixture(tmp_path)
    try:
        result=plugin.find_text(path='report.docx',revision=rev,query='r',scope='headers',limit=1);found=[]
        while True:
            found.extend((m['id'],m['start']) for m in result['matches'])
            cursor=result['next_cursor']
            if not cursor:break
            for scope in ['main','footers','all']:
                with pytest.raises(HarnessError,match='stale_search_cursor'):
                    plugin.find_text(path='report.docx',revision=rev,query='r',scope=scope,limit=1,cursor=cursor)
            result=plugin.find_text(path='report.docx',revision=rev,query='r',scope='headers',limit=1,cursor=cursor)
        assert found==[('paragraph:header:1:1',offset) for offset in [3,6,10,14]]
        with pytest.raises(ValidationError):FindText(path='report.docx',revision=rev,query='r',scope='everything')
    finally:store.close()
