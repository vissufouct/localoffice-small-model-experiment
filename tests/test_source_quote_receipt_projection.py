"""Quote receipt compaction requires exact execution and complete current evidence."""
import copy
import json

import pytest

from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical
from localoffice.word import content_plan, source_quotes
from localoffice.word.guidance import build_state
from localoffice.word.evidence import model_receipt
from test_native_objective_anchor import Provider
from test_word_content_plan import fixture


def setup(store,*,executed=True,legacy=False):
    source=source_quotes.candidates(store,'task')
    arguments={'source_hash':source['source_hash'],'selections':{'q01':'paragraph','q02':'header'}}
    action={'tool':'word.select_source_quotes',**arguments}
    if legacy:
        action={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Body'},
                                                     {'role':'header','quote':'Header'}]}
    call=record_call(store,'task',action)
    if legacy:receipt=content_plan.plan(store,'task',**action)
    else:receipt=source_quotes.select(store,'task',**arguments,native_call_id=call if executed else None)
    message=record_result(store,'task',call,receipt)
    with store.transaction():
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         ('word_tools:task',canonical(build_state(['prepare_creation']))))
    return call,message,receipt


def projected(messages,call):
    message=next(m for m in messages if m.get('tool_call_id')==call)
    return json.loads(message['content'].removeprefix('TOOL RECEIPT / SOURCE EVIDENCE: '))


@pytest.mark.asyncio
async def test_exact_quote_receipt_compacts_without_changing_durable_history(tmp_path):
    store=fixture(tmp_path,'Copy "Body" and header “Header”.')
    try:
        call,message,receipt=setup(store)
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        before=list(store.db.iterdump())
        messages=await runner.assemble('task')
        assert projected(messages,call)=={'status':'planned','document_changed':False,'details_in_current_state':True}
        assert list(store.db.iterdump())==before
        assert json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(message,)).fetchone()[0])==receipt
        assert 'ct_' in messages[0]['content']
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['truncated','missing_ref','wrong_role','wrong_text','preview','stale','stale_with_old_navigation'])
async def test_incomplete_or_stale_navigation_keeps_receipt_evidence(tmp_path,monkeypatch,case):
    store=fixture(tmp_path,'Copy "Body" and header “Header”.')
    try:
        call,_,receipt=setup(store)
        navigation=content_plan.binding_navigation(store,'task')
        first=receipt['literals'][0]
        if case=='truncated':navigation['truncated']=True
        elif case=='missing_ref':del navigation['by_role'][first['role']][first['ref']]
        elif case=='wrong_role':
            del navigation['by_role'][first['role']][first['ref']]
            navigation['by_role']['footer']={first['ref']:first['text']}
        elif case=='wrong_text':navigation['by_role'][first['role']][first['ref']]='Different text'
        elif case=='preview':navigation['by_role'][first['role']][first['ref']]={'preview':'Body','truncated':True}
        if case.startswith('stale'):
            with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed source' WHERE id='task'")
        if case!='stale':monkeypatch.setattr(content_plan,'binding_navigation',lambda *a,**kw:copy.deepcopy(navigation))
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        messages=await runner.assemble('task')
        assert projected(messages,call)==model_receipt(receipt)
        assert 'details_in_current_state' not in projected(messages,call)
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('executed,legacy',[(False,False),(True,True)])
async def test_unexecuted_or_other_tool_receipts_are_not_projected_as_quote_success(tmp_path,executed,legacy):
    store=fixture(tmp_path,'Copy "Body" and header “Header”.')
    try:
        call,_,receipt=setup(store,executed=executed,legacy=legacy)
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        assert projected(await runner.assemble('task'),call)==model_receipt(receipt)
    finally:store.close()


@pytest.mark.asyncio
async def test_modified_persisted_result_must_equal_exact_retained_receipt(tmp_path):
    store=fixture(tmp_path,'Copy "Body" and header “Header”.')
    try:
        call,message,receipt=setup(store)
        receipt={**receipt,'limitation':'Additional evidence must remain visible.'}
        with store.transaction():store.db.execute('UPDATE messages SET content=? WHERE id=?',(canonical(receipt),message))
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        assert projected(await runner.assemble('task'),call)==model_receipt(receipt)
    finally:store.close()


@pytest.mark.asyncio
async def test_long_current_literal_preview_does_not_hide_receipt(tmp_path):
    store=fixture(tmp_path,'Copy "'+'x'*81+'" and header “Header”.')
    try:
        call,_,receipt=setup(store)
        assert content_plan.binding_navigation(store,'task')['truncated'] is False
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        assert projected(await runner.assemble('task'),call)==model_receipt(receipt)
    finally:store.close()


@pytest.mark.asyncio
async def test_native_receipt_hash_corruption_fails_closed(tmp_path):
    store=fixture(tmp_path,'Copy "Body" and header “Header”.')
    try:
        call,_,_=setup(store)
        with store.transaction():
            store.db.execute('UPDATE meta SET value=? WHERE key=?',('{}',source_quotes.PREFIX+call))
        runner=TaskRunner(store,Provider(),source_planning=True,creation_review=True)
        with pytest.raises(HarnessError) as error:await runner.assemble('task')
        assert error.value.code=='state_corrupt'
    finally:store.close()
