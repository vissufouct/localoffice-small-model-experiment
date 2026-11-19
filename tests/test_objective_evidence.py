"""Exact source reconstruction, finite catalogue bounds and read-only resolution."""
import json
import pytest
from localoffice.word.objective_evidence import catalogue,catalogue_text,resolve,resolve_text,VERSION
from localoffice.storage import canonical,digest,ProjectStore
from localoffice.errors import HarnessError
from localoffice.chats import create_chat,select_chat,current_chat
from test_word_content_plan import fixture
from test_word_progress import task


@pytest.mark.parametrize('text',[
    'Create report.docx. Use 1.25 cm; keep “Olá 😀.” and "quote\\path".\r\nNext: exact Δ! End?',
    '  First.\n\nSecond;\tThird:\rFourth! Fifth?  ',
    'No punctuation and no inferred meaning',
    '重复。 café e\u0301 😀; more\u2028text\n',
    '"quoted. text"; escaped \\"text\\". next',
    ' \t\r\n ',
])
def test_exact_complete_reconstruction_and_codepoint_offsets(text):
    result=catalogue_text(text)
    assert result['available'] and result['complete']
    assert len(canonical(result).encode())<=2048
    segments=result['segments']
    assert list(segments)==[f's{i:02d}' for i in range(1,len(segments)+1)]
    assert ''.join(item['text'] for item in segments.values())==text
    previous=0
    for name,item in segments.items():
        assert item['start']==previous and item['end']>item['start']
        assert item['text']==text[item['start']:item['end']]
        selected=resolve_text(text,result['source_hash'],name)
        assert selected['text']==item['text']
        assert (selected['start'],selected['end'])==(item['start'],item['end'])
        previous=item['end']
    whole=resolve_text(text,result['source_hash'],'s01',list(segments)[-1])
    assert whole['text']==text and whole['start']==0 and whole['end']==len(text)


def test_mechanical_boundaries_and_repeated_spans_are_positions_not_unique_quotes():
    text='Repeat. Repeat. Tail'
    result=catalogue_text(text)
    assert [v['text'] for v in result['segments'].values()]==['Repeat. ','Repeat. ','Tail']
    first=resolve_text(text,result['source_hash'],'s01')
    second=resolve_text(text,result['source_hash'],'s02')
    assert first['text']==second['text'] and first['start']!=second['start']
    assert resolve_text(text,result['source_hash'],'s02','s03')['text']=='Repeat. Tail'
    assert catalogue_text('v1.2 file.docx no semantic guessing')['segments']['s01']['text']=='v1.2 file.docx no semantic guessing'


@pytest.mark.parametrize('start,end',[('s00',None),('s1',None),('s25',None),('s03',None),
    ('s02','s01'),('s01','s03'),('q01',None),(None,None),(True,None),('s01',''),('s01','s02\n')])
def test_invalid_or_reversed_ranges_fail(start,end):
    text='One. Two';revision=digest(text.encode())
    with pytest.raises(HarnessError) as exc:resolve_text(text,revision,start,end)
    assert exc.value.code=='objective_evidence_range'


@pytest.mark.parametrize('version',[0,2,True,'1',None])
def test_unknown_version_cannot_reinterpret_historical_ids(version):
    with pytest.raises(HarnessError,match='version'):catalogue_text('Text',version=version)
    with pytest.raises(HarnessError,match='version'):resolve_text('Text',digest(b'Text'),'s01',version=version)


def test_all_segments_or_explicit_fallback_no_truncation():
    assert catalogue_text('x; '*24)['available']
    result=catalogue_text('x; '*25)
    assert result['reason']=='segment_limit'
    for text,reason in [('', 'empty_source'),('x'*2048,'catalogue_byte_limit'),('😀'*500,'catalogue_byte_limit'),('x; '*25,'segment_limit')]:
        result=catalogue_text(text)
        assert result['available'] is False and result['complete'] is False
        assert result['segments']=={} and result['fallback']=='source_quote' and result['reason']==reason
        assert len(canonical(result).encode())<=2048
        with pytest.raises(HarnessError) as exc:resolve_text(text,result['source_hash'],'s01')
        assert exc.value.code=='objective_evidence_unavailable'


def test_serialized_utf8_boundary_includes_envelope_and_escaping():
    # Find the byte boundary for one indivisible source; no prefix is presented.
    available=[]
    for n in range(1700,2050):
        result=catalogue_text('x'*n)
        if result['available']:available.append(n)
    largest=max(available)
    exact=catalogue_text('x'*largest)
    assert len(canonical(exact).encode())<=2048
    assert not catalogue_text('x'*(largest+1))['available']
    assert not catalogue_text('\\"'*700)['available']  # JSON escaping also counts.


def test_source_guard_64k_and_invalid_unicode():
    assert catalogue_text('x'*65536)['reason']=='catalogue_byte_limit'
    for text in ['x'*65537,'😀'*16385,'\ud800']:
        with pytest.raises(HarnessError) as exc:catalogue_text(text)
        assert exc.value.code=='content_source'


def test_live_scope_freshness_restart_and_historical_resolution_without_writes(tmp_path):
    original='Margin: one inch. Draft 8–16 words.'
    store=fixture(tmp_path,original)
    try:
        before='\n'.join(store.db.iterdump())
        evidence=catalogue(store,'task')
        assert resolve(store,'task',evidence['source_hash'],'s01','s03')['text']==original
        assert '\n'.join(store.db.iterdump())==before
        store.close();store=ProjectStore(tmp_path)
        assert catalogue(store,'task')==evidence
        store.db.execute("UPDATE tasks SET objective='Different source' WHERE id='task'")
        with pytest.raises(HarnessError) as exc:resolve(store,'task',evidence['source_hash'],'s01')
        assert exc.value.code=='objective_evidence_stale'
        # Pure historical path uses only supplied original bytes, never current state.
        assert resolve_text(original,evidence['source_hash'],'s01','s03',version=VERSION)['text']==original
        chat=current_chat(store);other=create_chat(store);select_chat(store,other)
        for action in [lambda:catalogue(store,'task'),lambda:resolve(store,'task',evidence['source_hash'],'s01')]:
            with pytest.raises(HarnessError) as exc:action()
            assert exc.value.code=='chat_scope'
        select_chat(store,chat);task(store,'another')
        with pytest.raises(HarnessError) as exc:resolve(store,'another',evidence['source_hash'],'s01')
        assert exc.value.code=='objective_evidence_stale'
        with pytest.raises(HarnessError):catalogue(store,'missing')
    finally:store.close()
