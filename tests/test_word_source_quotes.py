"""Generic source quote selection and exact per-call receipts; no worker/model."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from localoffice.chats import create_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.storage import canonical, digest, ProjectStore
from localoffice.word import source_quotes as quotes
from localoffice.word import content_plan as plans
from test_word_content_plan import fixture
from test_word_progress import task


def args(store,selections=None):
    return {'source_hash':quotes.candidates(store,'task')['source_hash'],
            'selections':selections or {'q01':'paragraph'}}


def snapshot(store):
    return list(store.db.iterdump())


def test_balanced_unicode_spaces_and_source_order_are_exact_and_readonly(tmp_path):
    text='Use " café 😀 ", then “Δ = 2” and "  ".'
    store=fixture(tmp_path,text)
    try:
        before=snapshot(store);result=quotes.candidates(store,'task')
        assert result=={'available':True,'source_hash':digest(text.encode()),'omitted':False,'context_omitted':False,'candidates':{
            'q01':{'preview':' café 😀 ','text_chars':8,'truncated':False,'preceding_noncontent':'Use '},
            'q02':{'preview':'Δ = 2','text_chars':5,'truncated':False,'preceding_noncontent':'Use " café 😀 ", then '},
            'q03':{'preview':'  ','text_chars':2,'truncated':False,'preceding_noncontent':text[:text.index('"  "')][-128:]}}}
        assert snapshot(store)==before
        assert list(result['candidates'])==sorted(result['candidates'])
    finally:store.close()


@pytest.mark.parametrize('text,reason',[
    ('No quoted content.','no_quoted_passages'),('An "unfinished passage','unbalanced_quotes'),
    ('A ”closing only','unbalanced_quotes'),('“Unclosed curly','unbalanced_quotes'),
    ('"mixed closing”','ambiguous_quotes'),('“nested "passage" here”','ambiguous_quotes'),
    ('"a""b"','ambiguous_quotes'),('""','passage_length'),('“”','passage_length'),
    ('"a\\"b"','escaped_delimiter'),('Outside \\"quote"','escaped_delimiter'),
    (' '.join('"x"' for _ in range(25)),'candidate_limit'),
    ('"'+'a'*8001+'"','passage_length'),('x'*65537,'source_limit'),
    (' '.join('"'+str(n)+'漢'*79+'"' for n in range(8)),'projection_limit'),
    ('a '*1001+'"a"','occurrence_limit'),
])
def test_unavailable_is_complete_fallback_not_partial_candidates(tmp_path,text,reason):
    store=fixture(tmp_path,text)
    try:
        before=snapshot(store);result=quotes.candidates(store,'task')
        assert result['available'] is False and result['candidates']=={} and result['omitted'] is True
        assert result['reason']==reason and result['fallback']=='word.plan_content'
        assert result['source_hash']==digest(text.encode())
        assert len(canonical(result).encode())<=2048 and snapshot(store)==before
    finally:store.close()


def test_full_text_not_preview_is_selected_with_explicit_roles_and_spans(tmp_path):
    content='漢😀'*50
    text=f'Select "{content}" and “Tail ”.'
    store=fixture(tmp_path,text)
    try:
        items=quotes.candidates(store,'task')['candidates']
        assert items['q01']['preview']==content[:80] and items['q01']['text_chars']==100 and items['q01']['truncated']
        result=quotes.select(store,'task',**args(store,{'q01':'table_cell','q02':'footer'}))
        assert result['document_changed'] is False
        assert [(item['role'],item['text']) for item in result['literals']]==[('table_cell',content),('footer','Tail ')]
        for item in result['literals']:
            assert text[item['start']:item['end']]==item['text']
            assert plans.resolve(store,'task',item['ref'],role=item['role'])==item['text']
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def test_identical_quotes_bind_distinct_real_occurrences_including_unquoted_matches(tmp_path):
    text='echo and "echo" then “echo”; echo'
    store=fixture(tmp_path,text)
    try:
        result=quotes.select(store,'task',**args(store,{'q01':'paragraph','q02':'paragraph'}))
        items=result['literals']
        assert [item['start'] for item in items]==[text.index('"echo"')+1,text.index('“echo”')+1]
        assert items[0]['ref']!=items[1]['ref'] and all(item['text']=='echo' for item in items)
        assert len(plans.active_plan(store,'task')['refs'])==2
    finally:store.close()


@pytest.mark.parametrize('change',['hash','unknown_id','role_limit','request_limit'])
def test_invalid_selection_is_atomic(tmp_path,change):
    text='"'+'漢'*6000+'" and "'+'字'*6000+'"' if change=='request_limit' else '"'+'a'*2100+'"'
    store=fixture(tmp_path,text)
    try:
        request=args(store)
        if change=='hash':request['source_hash']='0'*64
        elif change=='unknown_id':request['selections']={'q24':'paragraph'}
        elif change=='role_limit':request['selections']={'q01':'footer'}
        else:request['selections']={'q01':'paragraph','q02':'paragraph'}
        before=snapshot(store)
        with pytest.raises((HarnessError,ValidationError)):quotes.select(store,'task',**request)
        assert snapshot(store)==before
    finally:store.close()


def test_changed_source_rejects_old_candidate_ids(tmp_path):
    store=fixture(tmp_path,'"First"')
    try:
        request=args(store)
        with store.transaction():store.db.execute("UPDATE tasks SET objective='\"Other\"' WHERE id='task'")
        before=snapshot(store)
        with pytest.raises(HarnessError) as error:quotes.select(store,'task',**request)
        assert error.value.code=='content_source_stale' and snapshot(store)==before
    finally:store.close()


@pytest.mark.parametrize('api',['candidates','select','result'])
def test_wrong_owner_never_exposes_sources(tmp_path,api):
    store=fixture(tmp_path,'"Private"')
    try:
        request=args(store);call=record_call(store,'task',{'tool':'word.select_source_quotes',**request})
        select_chat(store,create_chat(store))
        with pytest.raises(HarnessError):
            if api=='candidates':quotes.candidates(store,'task')
            elif api=='select':quotes.select(store,'task',**request)
            else:quotes.result(store,'task',request,native_call_id=call)
    finally:store.close()


def test_native_receipt_replay_after_restart_is_historical_no_source_or_plan_calls(tmp_path,monkeypatch):
    store=fixture(tmp_path,'"Quoted"')
    request=args(store);call=record_call(store,'task',{'tool':'word.select_source_quotes',**request})
    assert quotes.result(store,'task',request,native_call_id=call) is None
    receipt=quotes.select(store,'task',**request,native_call_id=call)
    record_result(store,'task',call,receipt)
    with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
    store.close();store=ProjectStore(tmp_path)
    try:
        def forbidden(*a,**kw):pytest.fail('Historical receipt retrieval must not resolve sources or plan')
        monkeypatch.setattr(plans,'source',forbidden);monkeypatch.setattr(plans,'plan',forbidden)
        before=snapshot(store)
        assert quotes.result(store,'task',request,native_call_id=call)==receipt
        assert quotes.select(store,'task',**request,native_call_id=call)==receipt
        assert snapshot(store)==before
    finally:store.close()


def test_identical_unexecuted_second_native_call_has_no_receipt(tmp_path):
    store=fixture(tmp_path,'"Quoted"')
    try:
        request=args(store);action={'tool':'word.select_source_quotes',**request}
        first=record_call(store,'task',action)
        receipt=quotes.select(store,'task',**request,native_call_id=first)
        record_result(store,'task',first,receipt)
        second=record_call(store,'task',action)
        assert quotes.result(store,'task',request,native_call_id=second) is None
        assert quotes.select(store,'task',**request,native_call_id=second)==receipt
        assert quotes.result(store,'task',request,native_call_id=first)==receipt
    finally:store.close()


@pytest.mark.parametrize('mismatch',['tool','args','task','unknown','completed'])
def test_native_capture_requires_matching_pending_call(tmp_path,mismatch):
    store=fixture(tmp_path,'"Quoted"')
    try:
        request=args(store);action={'tool':'word.select_source_quotes',**request};key='task'
        if mismatch=='tool':action={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Quoted'}]}
        elif mismatch=='args':action={**action,'selections':{'q01':'header'}}
        elif mismatch=='task':task(store,'other');key='other'
        call=record_call(store,key,action)
        if mismatch=='unknown':call='call_'+'0'*32
        elif mismatch=='completed':record_result(store,key,call,{'status':'other'})
        before=snapshot(store)
        with pytest.raises(HarnessError):quotes.select(store,'task',**request,native_call_id=call)
        assert snapshot(store)==before
    finally:store.close()


@pytest.mark.parametrize('part',['envelope','receipt','request'])
def test_tampered_receipt_fails_before_replay(tmp_path,part):
    store=fixture(tmp_path,'"Quoted"')
    try:
        request=args(store);call=record_call(store,'task',{'tool':'word.select_source_quotes',**request})
        quotes.select(store,'task',**request,native_call_id=call)
        key=quotes.PREFIX+call
        envelope=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
        if part=='request':envelope['state']['request']['selections']['q01']='header'
        else:envelope['state']['receipt']['status']='fabricated'
        if part!='envelope':envelope['hash']=digest(canonical(envelope['state']).encode())
        with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),key))
        before=snapshot(store)
        with pytest.raises(HarnessError) as error:quotes.result(store,'task',request,native_call_id=call)
        assert error.value.code=='state_corrupt' and snapshot(store)==before
    finally:store.close()


def test_native_receipt_failure_rolls_back_planning(tmp_path):
    store=fixture(tmp_path,'"Quoted"')
    try:
        request=args(store);call=record_call(store,'task',{'tool':'word.select_source_quotes',**request})
        with store.transaction():store.db.execute("CREATE TRIGGER reject_quote_receipt BEFORE INSERT ON meta WHEN NEW.key LIKE 'word_source_quote_selection:%' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        before=snapshot(store)
        with pytest.raises(Exception,match='interrupted'):quotes.select(store,'task',**request,native_call_id=call)
        assert snapshot(store)==before
    finally:store.close()


@pytest.mark.parametrize('selections',[{}, {'q1':'paragraph'}, {'q00':'header'}, {'q25':'footer'},
                                     {'q01':'invented'}, {'q01':['header','footer']}])
def test_finite_selection_contract(selections):
    with pytest.raises(ValidationError):quotes.SourceQuoteSelection(source_hash='a'*64,selections=selections)


def test_all_24_candidates_fit_in_numeric_order_and_remain_distinct(tmp_path):
    store=fixture(tmp_path,' and '.join('"x"' for _ in range(24)))
    try:
        available=quotes.candidates(store,'task')
        assert available['available'] and not available['omitted']
        assert list(available['candidates'])==[f'q{n:02d}' for n in range(1,25)]
        assert len(canonical(available).encode())<=2048
        receipt=quotes.select(store,'task',**args(store,{key:'paragraph' for key in available['candidates']}))
        assert len({item['ref'] for item in receipt['literals']})==24
    finally:store.close()


def test_non_delimiter_backslashes_remain_literal(tmp_path):
    text=r'Use "C:\docs\notes" as literal text.'
    store=fixture(tmp_path,text)
    try:
        assert quotes.candidates(store,'task')['available']
        receipt=quotes.select(store,'task',**args(store))
        assert receipt['literals'][0]['text']==r'C:\docs\notes'
    finally:store.close()


def test_preceding_context_is_verbatim_bounded_and_never_selected_content(tmp_path):
    prefix='😀漢'*30+'\nFooter label:  '
    text=prefix+'“ Page ”'
    store=fixture(tmp_path,text)
    try:
        available=quotes.candidates(store,'task');item=available['candidates']['q01']
        assert item['preceding_noncontent']==prefix[-128:]
        assert len(item['preceding_noncontent'])<=128
        assert item['preview']==' Page ' and 'role' not in item
        receipt=quotes.select(store,'task',**args(store,{'q01':'header'}))
        assert receipt['literals'][0]['text']==' Page '
        assert receipt['literals'][0]['role']=='header'  # Context never overrides the explicit role.
    finally:store.close()


def test_context_overflow_omits_context_not_candidates(tmp_path):
    text=' '.join('Header text "'+str(n)+'"' for n in range(24))
    store=fixture(tmp_path,text)
    try:
        available=quotes.candidates(store,'task')
        assert available['available'] and available['omitted'] is False
        assert available['context_omitted'] is True
        assert len(available['candidates'])==24
        assert len(canonical(available).encode())<=2048
        assert any('preceding_noncontent' not in item for item in available['candidates'].values())
        for key,item in available['candidates'].items():assert item['preview']==str(int(key[1:])-1)
    finally:store.close()


def test_context_omission_flag_does_not_make_old_near_limit_candidates_unavailable(tmp_path):
    # Construct a projection admitted under the original 2048-byte bound, with
    # too little headroom for even the new omission flag.
    for width in range(1,81):
        texts=[str(n)+'x'*width for n in range(24)]
        objective=' '.join('"'+text+'"' for text in texts)
        old={'available':True,'source_hash':digest(objective.encode()),'omitted':False,
             'candidates':{f'q{n+1:02d}':{'preview':text,'truncated':False,'text_chars':len(text)}
                           for n,text in enumerate(texts)}}
        if 2025<=len(canonical(old).encode())<=2048:break
    else:pytest.fail('Missing near-boundary fixture')
    store=fixture(tmp_path,objective)
    try:
        available=quotes.candidates(store,'task')
        assert available['available'] and available['context_omitted']
        assert set(available['candidates'])==set(old['candidates'])
        assert len(canonical(available).encode())<=2048
        for key,item in available['candidates'].items():
            assert old['candidates'][key]['preview'].startswith(item['preview'])
            if item['preview']!=old['candidates'][key]['preview']:assert item['truncated']
    finally:store.close()


@pytest.mark.parametrize('case',['C1','C2'])
def test_frozen_fixture_candidate_context_bytes(tmp_path,monkeypatch,case):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    from benchmark_word_creation_cases import prompt_for
    store=fixture(tmp_path,prompt_for(case))
    try:
        available=quotes.candidates(store,'task')
        assert available['available'] and available['omitted'] is False
        assert len(canonical(available).encode())<=2048
        count=len(available['candidates'])
        contexts=sum('preceding_noncontent' in item for item in available['candidates'].values())
        print(f'{case}: candidate_bytes={len(canonical(available).encode())}, candidates={count}, contexts={contexts}, context_omitted={available["context_omitted"]}')
    finally:store.close()


def test_context_preserves_role_word_beyond_old_short_window(tmp_path):
    prefix='The header must be right-aligned and contain exactly '
    store=fixture(tmp_path,prefix+'"Exact label"')
    try:
        result=quotes.candidates(store,'task')
        assert result['candidates']['q01']['preceding_noncontent']==prefix
        assert 'role' not in result['candidates']['q01']
        assert len(canonical(result).encode())<=2048
    finally:store.close()
