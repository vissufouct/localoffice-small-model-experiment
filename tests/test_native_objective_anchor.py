"""Protected user objective under native pruning; tokenizer only, never inference."""
import json
from pathlib import Path

import pytest

from localoffice.chats import create_chat, select_chat
from localoffice.errors import HarnessError
from localoffice.native_history import record_call, record_result
from localoffice.native_tools import render_prompt, validate_history
from localoffice.runtime import TaskRunner
from localoffice.storage import canonical
from localoffice.word.guidance import build_state
from test_word_content_plan import fixture
from test_word_progress import task


class Provider:
    native_tool_protocol=True
    context_limit=4096

    def __init__(self,count=None):
        self.count=count or (lambda messages:500)
        self.seen=[]

    async def count_tokens(self,messages,**kwargs):
        if self.native_tool_protocol:validate_history(messages)
        self.seen.append(json.loads(json.dumps(messages)))
        return self.count(messages)


def exchange(store,label):
    call=record_call(store,'task',{'tool':'word.load_tools','operations':['plan_content']})
    record_result(store,'task',call,{'label':label})
    return call


@pytest.mark.asyncio
async def test_forced_pruning_retains_objective_and_whole_latest_native_pair(tmp_path):
    objective='Use 12 pt, exact margins, and café — preserve this request.'
    store=fixture(tmp_path,objective)
    try:
        store.message('user',objective,'task')
        older=exchange(store,'older');latest=exchange(store,'latest')
        provider=Provider(lambda messages:8000 if len(messages)>4 else 500)
        runner=TaskRunner(store,provider,source_planning=True)
        messages=await runner.assemble('task')
        assert [m['role'] for m in messages]==['system','user','assistant','tool']
        assert messages[1]['content']==objective
        assert objective not in messages[0]['content']
        assert sum(m.get('content')==objective for m in messages)==1
        assert messages[2]['tool_calls'][0]['id']==latest
        assert older not in canonical(messages)
        assert runner.context_info['omitted_messages']==2
        for counted in provider.seen:
            assert counted[1]=={'role':'user','content':objective}
            validate_history(counted)
    finally:store.close()


@pytest.mark.asyncio
async def test_only_exact_current_task_duplicate_removed_continuations_preserved(tmp_path):
    objective='Original exact task'
    store=fixture(tmp_path,objective)
    try:
        task(store,'other')
        store.message('user',objective,'other')
        store.message('user',objective,'task')
        annotated=objective+'\nUser-selected annotation instructions: use 14 pt in the selected paragraph.'
        continuation='Correction: use the supplied heading unchanged.'
        store.message('user',annotated,'task');store.message('user',continuation,'task')
        runner=TaskRunner(store,Provider(),source_planning=True)
        messages=await runner.assemble('task')
        users=[m['content'] for m in messages if m['role']=='user']
        assert users==[objective,objective,annotated,continuation]
        assert '"objective":' not in messages[0]['content']
        assert store.db.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0]==4
    finally:store.close()


@pytest.mark.asyncio
async def test_objective_survives_history_floor_and_missing_recent_user_message(tmp_path):
    objective='Original task outside the recent window'
    store=fixture(tmp_path,objective)
    try:
        first=store.message('user',objective,'task')
        runner=TaskRunner(store,Provider(),source_planning=True)
        with store.transaction():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('history_floor:'+runner.chat_id,str(first)))
        exchange(store,'latest')
        messages=await runner.assemble('task')
        assert messages[1]=={'role':'user','content':objective}
        assert [m['role'] for m in messages]==['system','user','assistant','tool']
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['running','completed'])
async def test_anchor_remains_after_candidate_exists(tmp_path,status):
    from localoffice.word.contracts import CreateDocument
    from test_creation_evidence import package, para
    objective='Create and retain this exact objective through completion.'
    store=fixture(tmp_path,objective)
    try:
        request=CreateDocument(path='report.docx',blocks=[{'kind':'paragraph','text':'Body'}])
        store._stage_validated_bytes(path='report.docx',data=package(para('Body')),expected_revision=None,
            idempotency_key='candidate',task_id='task',tool='word.stage',
            args={'request':request.model_dump(),'task_id':'task'},validation={'results':[],'creation':True})
        runner=TaskRunner(store,Provider(),source_planning=True)
        with store.transaction():store.db.execute('UPDATE tasks SET status=? WHERE id=?',(status,'task'))
        messages=await runner.assemble('task')
        assert messages[1]=={'role':'user','content':objective}
        assert '"objective":' not in messages[0]['content']
    finally:store.close()


@pytest.mark.asyncio
async def test_other_chat_cannot_anchor_task_objective(tmp_path):
    store=fixture(tmp_path,'Private objective')
    try:
        select_chat(store,create_chat(store))
        provider=Provider();runner=TaskRunner(store,provider,source_planning=True)
        with pytest.raises(HarnessError) as error:await runner.assemble('task')
        assert error.value.code=='chat_scope' and provider.seen==[]
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('native,source_planning',[(True,False),(False,True),(False,False)])
async def test_other_modes_keep_system_objective_and_existing_history(tmp_path,native,source_planning):
    objective='Unchanged mode objective'
    store=fixture(tmp_path,objective)
    try:
        provider=Provider();provider.native_tool_protocol=native
        runner=TaskRunner(store,provider,source_planning=source_planning)
        messages=await runner.assemble('task')
        assert objective in messages[0]['content'] and '"objective":' in messages[0]['content']
        assert len(messages)==1
    finally:store.close()


@pytest.mark.asyncio
async def test_oversized_anchor_rejected_without_truncation(tmp_path):
    objective='Large exact objective '*1000
    store=fixture(tmp_path,objective)
    try:
        provider=Provider(lambda messages:len(messages[1]['content']))
        runner=TaskRunner(store,provider,source_planning=True)
        with pytest.raises(HarnessError) as error:await runner.assemble('task')
        assert error.value.code=='context_capacity'
        assert all(m[1]['content']==objective for m in provider.seen)
    finally:store.close()


@pytest.mark.asyncio
async def test_actual_pinned_tokenizer_admits_initial_and_loaded_prepare(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    approval_path=root/'docs/benchmarks/approved-mlx-download.json'
    if not approval_path.exists():pytest.skip('Local approved tokenizer not installed')
    approval=json.loads(approval_path.read_text());path=Path(approval['local_path'])
    if not (path/'tokenizer.json').exists():pytest.skip('Local approved tokenizer not installed')
    assert approval['revision']==path.name=='50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
    monkeypatch.setenv('HF_HUB_OFFLINE','1');monkeypatch.setenv('TRANSFORMERS_OFFLINE','1')
    monkeypatch.setenv('TOKENIZERS_PARALLELISM','false')
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(str(path),local_files_only=True,trust_remote_code=False)
    monkeypatch.syspath_prepend(str(root/'scripts'))
    from benchmark_word_creation_cases import prompt_for
    from probe_word_creation_cases_admission import selections
    from localoffice.word.content_plan import plan
    class TokenizerProvider(Provider):
        async def count_tokens(self,messages,*,tools):
            validate_history(messages)
            return len(tokenizer.encode(render_prompt(tokenizer,messages,tools=tools)))
    objective=prompt_for('C1');store=fixture(tmp_path,objective)
    try:
        runner=TaskRunner(store,TokenizerProvider(),source_planning=True,creation_review=True)
        skill=runner.skills.default_word(True)
        with store.transaction():
            store.db.execute('INSERT INTO task_skills VALUES(?,?,?,?,?)',
                ('task',skill['id'],skill['source_hash'],skill['name'],skill['body']))
        store.message('user',objective,'task')
        initial=await runner.assemble('task')
        initial_count=runner.context_info['input_tokens']
        plan(store,'task',literals=selections('C1'))
        prepared=await runner.assemble('task',word_tool_override=build_state(['prepare_creation']))
        assert initial[1]==prepared[1]=={'role':'user','content':objective}
        assert initial_count<=3200 and runner.context_info['input_tokens']<=3200
        assert runner.context_info['output_reserve']==768 and runner.context_info['safety_margin']==128
        print(f'Offline tokenizer only: initial={initial_count}, prepare={runner.context_info["input_tokens"]}')
    finally:store.close()
