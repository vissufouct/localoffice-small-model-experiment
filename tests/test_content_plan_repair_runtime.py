"""Explicit source repair survives native execution and receipt recovery."""
import json
import io
import zipfile
from xml.etree import ElementTree as ET
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.word.plugin import WordPlugin
from localoffice.word.content_plan_repair import pending
from localoffice.native_tools import content_plan_state_tools,validate_native_arguments
from localoffice.native_history import record_call,reconcile_word_call
from localoffice.errors import HarnessError
from test_native_runner import NativeProvider
from test_word_content_plan import fixture

BAD={'literals':[{'role':'paragraph','quote':'Copied.'},
                 {'role':'paragraph','quote':'Please review this document when you have time.'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('navigation_evidence',['complete','truncated_sources','stale_drafts'])
async def test_failed_quote_explicit_defer_then_creation_finishes_with_draft_and_copy(tmp_path,monkeypatch,navigation_evidence):
    store=ProjectStore(tmp_path)
    if navigation_evidence=='truncated_sources':
        from localoffice.word import content_plan
        original=content_plan.binding_navigation
        def partial(*args,**kwargs):
            result=original(*args,**kwargs)
            return {**result,'truncated':True} if result else result
        monkeypatch.setattr(content_plan,'binding_navigation',partial)
    elif navigation_evidence=='stale_drafts':
        from localoffice.word import content_plan_repair
        original=content_plan_repair.draft_summary
        def stale(*args,**kwargs):
            result=original(*args,**kwargs)
            if result:
                for item in result['items']:
                    item['fresh']=False;item.pop('preview',None);item.pop('truncated',None)
            return result
        monkeypatch.setattr(content_plan_repair,'draft_summary',stale)
    class Provider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            key=store.db.execute('SELECT id FROM tasks').fetchone()[0]
            names={t['function']['name'] for t in tools}
            if self.calls==0:action={'tool':'word.plan_content',**BAD}
            elif self.calls==1:
                assert 'word.repair_content_plan' in names
                assert 'word.plan_content' not in names
                state=pending(store,key)
                assert state['status']=='pending'
                action={'tool':'word.repair_content_plan','ref':state['ref'],
                        'changes':[{'op':'defer_as_draft','index':1}]}
                wrong={k:v for k,v in action.items() if k!='tool'};wrong['ref']='cp_'+'f'*32
                with pytest.raises(HarnessError):validate_native_arguments(action['tool'],wrong,tools)
            elif self.calls==2:
                assert 'unbound_draft_decisions' in messages[0]['content']
                projected=json.loads(messages[-1]['content'].split('TOOL RECEIPT / SOURCE EVIDENCE: ',1)[1])
                if navigation_evidence=='complete':assert projected['details_in_current_state'] is True
                else:assert 'details_in_current_state' not in projected and projected['references']
                saved=json.loads(store.db.execute("SELECT content FROM messages WHERE task_id=? AND role='tool' ORDER BY id DESC LIMIT 1",(key,)).fetchone()[0])
                assert saved['references'] and saved['draft_indices']==[1]
                assert 'details_in_current_state' not in saved
                assert 'word.prepare_creation' in names and 'word.plan_content' not in names
                assert store.candidate('report.docx') is None
                loader=next(t['function'] for t in tools if t['function']['name']=='word.load_tools')
                assert 'plan_content' in loader['parameters']['properties']['operations']['items']['enum']
                action={'tool':'word.load_tools','operations':['prepare_creation']}
            elif self.calls==3:
                action={'tool':'word.prepare_creation','path':'report.docx','blocks':[
                    {'kind':'paragraph','text':'Copied.'},
                    {'kind':'paragraph','text':BAD['literals'][1]['quote']}]}
            elif self.calls==4:
                from localoffice.word.creation_repair import pending as creation_pending
                action={'tool':'word.finalize_creation','ref':creation_pending(store,key)['ref'],'styles':[]}
            else:
                assert self.calls==5
                action={'tool':'finish','text':'Ready for review.'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action['tool'],
                'arguments':{k:v for k,v in action.items() if k!='tool'}})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    provider=Provider(None);runner=TaskRunner(store,provider,source_planning=True,creation_review=True)
    try:
        key=await runner.start('Create exactly Copied. Then draft an invitation to review.',artifact='report.docx',mode='background')
        await runner.job
        assert store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]=='completed',runner.events
        assert provider.calls==6
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==1
        candidate=store.candidate('report.docx')
        with zipfile.ZipFile(io.BytesIO(store.blob(candidate['candidate_hash']))) as z:
            root=ET.fromstring(z.read('word/document.xml'))
            assert [n.text for n in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')]==['Copied.',BAD['literals'][1]['quote']]
        assert not (tmp_path/'report.docx').exists()
    finally:await runner.pause();store.close()


def test_native_recovery_uses_saved_repair_receipt_without_reexecution(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Copied. Draft an invitation.')
    try:
        with pytest.raises(HarnessError):WordPlugin(store).plan_content(task_id='task',**BAD)
        ref=pending(store,'task')['ref']
        action={'tool':'word.repair_content_plan','ref':ref,'changes':[{'op':'defer_as_draft','index':1}]}
        record_call(store,'task',action)
        receipt=WordPlugin(store).repair_content_plan(task_id='task',**{k:v for k,v in action.items() if k!='tool'})
        def forbidden(*args,**kwargs):raise AssertionError('No repair replay during recovery')
        monkeypatch.setattr('localoffice.word.content_plan_repair.repair',forbidden)
        monkeypatch.setattr('localoffice.word.content_plan.plan',forbidden)
        result=reconcile_word_call(store,'task')
        assert result['state']=='reconciled'
        saved=json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(result['message_id'],)).fetchone()[0])
        assert saved==receipt
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert reconcile_word_call(store,'task')['state']=='no_pending_call'
    finally:store.close()


def test_native_recovery_does_not_infer_an_unexecuted_correction(tmp_path):
    store=fixture(tmp_path,'Copied. Draft an invitation.')
    try:
        with pytest.raises(HarnessError):WordPlugin(store).plan_content(task_id='task',**BAD)
        ref=pending(store,'task')['ref']
        record_call(store,'task',{'tool':'word.repair_content_plan','ref':ref,
                                'changes':[{'op':'defer_as_draft','index':1}]})
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()


def test_forget_removes_retained_failed_text_and_draft_decisions(tmp_path):
    from localoffice.memory import forget_project_memory
    store=fixture(tmp_path,'Copied. Draft an invitation.')
    try:
        call_id=record_call(store,'task',{'tool':'word.plan_content',**BAD})
        with pytest.raises(HarnessError):WordPlugin(store).plan_content(task_id='task',native_call_id=call_id,**BAD)
        assert store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'word_content_plan_failure:%'").fetchone()[0]==1
        ref=pending(store,'task')['ref']
        WordPlugin(store).repair_content_plan(task_id='task',ref=ref,changes=[{'op':'defer_as_draft','index':1}])
        forget_project_memory(store,'FORGET PROJECT MEMORY')
        for prefix in ('word_content_plan_repair:','word_pending_content_plan:','word_draft_decisions:','word_content_plan_failure:'):
            assert store.db.execute('SELECT count(*) FROM meta WHERE key LIKE ?',(prefix+'%',)).fetchone()[0]==0
    finally:store.close()


def test_native_failed_plan_receipt_recovers_as_failure_not_execution(tmp_path,monkeypatch):
    store=fixture(tmp_path,'Copied. Draft an invitation.')
    try:
        call_id=record_call(store,'task',{'tool':'word.plan_content',**BAD})
        with pytest.raises(HarnessError) as exc:WordPlugin(store).plan_content(task_id='task',native_call_id=call_id,**BAD)
        expected={'code':exc.value.code,'message':str(exc.value)[:1200],'recovery':exc.value.recovery}
        def forbidden(*args,**kwargs):raise AssertionError('Never execute failed source selection on recovery')
        monkeypatch.setattr('localoffice.word.content_plan.plan',forbidden)
        monkeypatch.setattr('localoffice.word.content_plan_repair.capture',forbidden)
        result=reconcile_word_call(store,'task')
        assert result['state']=='reconciled' and result['outcome']=='recorded_failure'
        saved=json.loads(store.db.execute('SELECT content FROM messages WHERE id=?',(result['message_id'],)).fetchone()[0])
        assert saved==expected
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
        from localoffice.recovery import load
        from localoffice.chats import current_chat
        assert load(store,'task',current_chat(store))['unresolved'] is True
        assert load(store,'task',current_chat(store))['mutation'] is False
    finally:store.close()


def test_repair_grammar_pins_handle_but_leaves_decision_and_exit_to_model():
    import re
    from localoffice.word.guidance import project_schema
    from localoffice.runtime import WORD_ACTION
    from localoffice.native_tools import action_tools,compact_tools
    from localoffice.decoding import native_tool_regex
    ref='cp_'+'a'*32
    tools=content_plan_state_tools(compact_tools(action_tools(project_schema(WORD_ACTION.json_schema(),['repair_content_plan']))),
                                   {'status':'pending','ref':ref,'selection_count':2})
    regex=native_tool_regex(tools)
    def match(args,name='word.repair_content_plan'):
        return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':name,'arguments':args})+'</tool_call>')
    assert match({'ref':ref,'changes':[{'op':'defer_as_draft','index':1}]})
    assert match({'ref':ref,'changes':[{'op':'replace','index':1,'selection':{'role':'paragraph','quote':'Copied.'}}]})
    assert not match({'ref':'cp_'+'b'*32,'changes':[{'op':'defer_as_draft','index':1}]})
    assert not match({'ref':ref,'changes':[{'op':'remove','index':1}]})
    assert match({'text':'A genuine missing fact?'},'ask_user')


def test_same_arguments_new_unexecuted_call_cannot_borrow_previous_failure(tmp_path):
    from localoffice.native_history import record_result
    store=fixture(tmp_path,'Copied. Draft an invitation.')
    try:
        action={'tool':'word.plan_content',**BAD}
        first=record_call(store,'task',action)
        with pytest.raises(HarnessError) as exc:
            WordPlugin(store).plan_content(task_id='task',native_call_id=first,**BAD)
        old_ref=pending(store,'task')['ref']
        failure={'code':exc.value.code,'message':str(exc.value),'recovery':exc.value.recovery}
        record_result(store,'task',first,failure)
        second=record_call(store,'task',action)
        assert reconcile_word_call(store,'task')['state']=='unresolved'
        assert store.db.execute('SELECT result_message FROM native_calls WHERE call_id=?',(second,)).fetchone()[0] is None
        # Only actual execution and capture of B establishes its failure.
        with pytest.raises(HarnessError):
            WordPlugin(store).plan_content(task_id='task',native_call_id=second,**BAD)
        assert pending(store,'task')['ref']==old_ref
        result=reconcile_word_call(store,'task')
        assert result['state']=='reconciled' and result['call_id']==second
        assert result['outcome']=='recorded_failure'
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==0
    finally:store.close()
