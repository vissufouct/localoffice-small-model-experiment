"""Existing targets expose editing; lifecycle projections do not mutate saved state."""
import copy
import json
import re

import pytest

from localoffice.runtime import TaskRunner, CREATION_ONLY_OPERATIONS, _guard_existing_creation_load
from localoffice.storage import canonical
from localoffice.decoding import native_tool_regex
from localoffice.native_tools import validate_native_arguments
from localoffice.errors import HarnessError
from localoffice.word.guidance import build_state
from localoffice.word.creation_repair import capture
from test_word_content_plan import fixture
from test_native_runner import NativeProvider


def lifecycle(store, mode):
    if mode == 'absent':return
    if mode == 'external':
        (store.root/'report.docx').write_bytes(b'External fixture, no worker execution')
        return
    op=store._stage_validated_bytes(path='report.docx',data=b'Lifecycle fixture, no worker execution',
        expected_revision=None,idempotency_key='fixture',tool='word.stage',args={'fixture':True},validation={})
    if mode == 'staged':return
    store.apply(op['id'],expected_candidate=op['candidate_hash'],action='save')
    if mode == 'saved':return
    removal=store.undo(op['id'],'remove')
    if mode == 'pending_undo':return
    store.apply(removal['id'],expected_candidate=removal['candidate_hash'],action='remove')
    if mode == 'removed':return
    restored=store.undo(removal['id'],'restore')
    if mode == 'restore_candidate':return
    store.apply(restored['id'],expected_candidate=restored['candidate_hash'],action='save')


def snapshot(store):
    return {name:[tuple(row) for row in store.db.execute('SELECT * FROM '+name)]
            for name in ('meta','operations','candidates')}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['staged','saved','restored','external','pending_undo','restore_candidate'])
@pytest.mark.parametrize('override',[False,True])
async def test_existing_target_filters_before_guides_and_native_schemas_without_writes(tmp_path,mode,override):
    store=fixture(tmp_path)
    runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
    try:
        capture(store,'task',{'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}]},allow_valid=True)
        lifecycle(store,mode)
        selected=build_state(['prepare_creation','revise_creation','set_section_layout','inspect'])
        with store.transaction():store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(selected)))
        before=snapshot(store);original=copy.deepcopy(selected)
        messages=await runner.assemble('task',**({'word_tool_override':selected} if override else {}))
        assert runner.word_tool_state['operations']==['set_section_layout','inspect']
        assert set(runner.word_tool_state['guidance'])=={'set_section_layout','inspect'}
        assert selected==original and snapshot(store)==before
        names={t['function']['name'] for t in runner.native_tools}
        assert not names & {'word.'+n for n in CREATION_ONLY_OPERATIONS}
        assert {'ask_user','finish','word.list_objects','word.find_text','word.read_objects','read_file','read_pdf','word.stage_batch'}<=names
        loader=next(t['function']['parameters'] for t in runner.native_tools if t['function']['name']=='word.load_tools')
        choices=loader['properties']['operations']['items']['enum']
        assert not set(choices)&CREATION_ONLY_OPERATIONS
        assert {'set_section_layout','set_style','format_paragraph','plan_content','inspect'}<=set(choices)
        for name in CREATION_ONLY_OPERATIONS:
            with pytest.raises(HarnessError):validate_native_arguments('word.load_tools',{'operations':[name]},runner.native_tools)
        validate_native_arguments('word.load_tools',{'operations':['set_section_layout']},runner.native_tools)
        # Compile just the actual loader plus control, avoiding unrelated editing grammars.
        subset=[t for t in runner.native_tools if t['function']['name'] in ('word.load_tools','ask_user')]
        regex=native_tool_regex(subset)
        for operation in [*CREATION_ONLY_OPERATIONS,'set_section_layout','inspect','plan_content']:
            raw='<tool_call>'+json.dumps({'name':'word.load_tools','arguments':{'operations':[operation]}},separators=(',',':'))+'</tool_call>'
            assert bool(re.fullmatch(regex,raw))==(operation not in CREATION_ONLY_OPERATIONS)
        system=messages[0]['content']
        assert '\nrevise_creation: ' not in system
        assert '"creation_repair":' not in system
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['absent','removed'])
async def test_actual_absence_preserves_creation_and_stale_reroute(tmp_path,mode):
    store=fixture(tmp_path);runner=TaskRunner(store,NativeProvider(None),source_planning=True,creation_review=True)
    try:
        capture(store,'task',{'path':'report.docx','blocks':[{'kind':'paragraph','text':'Body'}]},allow_valid=True)
        lifecycle(store,mode)
        with store.transaction():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('word_tools:task',canonical(build_state(['finalize_creation']))))
            store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        before=snapshot(store)
        await runner.assemble('task')
        assert runner.word_tool_state['operations']==['prepare_creation']
        assert 'word.prepare_creation' in {t['function']['name'] for t in runner.native_tools}
        assert snapshot(store)==before
    finally:await runner.pause();store.close()


@pytest.mark.parametrize('mode',['staged','saved','restored','external','pending_undo','restore_candidate','absent','removed'])
def test_load_time_guard_rechecks_presence_without_partial_selection_or_mutation(tmp_path,mode):
    store=fixture(tmp_path)
    try:
        lifecycle(store,mode);before=snapshot(store)
        for creation in CREATION_ONLY_OPERATIONS:
            if mode in ('absent','removed'):_guard_existing_creation_load(store,'report.docx',[creation,'set_section_layout'])
            else:
                with pytest.raises(HarnessError,match='Target already exists'):
                    _guard_existing_creation_load(store,'report.docx',[creation,'set_section_layout'])
        _guard_existing_creation_load(store,'report.docx',['set_section_layout','inspect','plan_content'])
        assert snapshot(store)==before
    finally:store.close()


@pytest.mark.asyncio
async def test_external_creation_after_prompt_rejects_entire_native_load(tmp_path):
    from localoffice.storage import ProjectStore
    store=ProjectStore(tmp_path)
    class RaceProvider(NativeProvider):
        async def stream(self,messages,*,tools,**kwargs):
            self.calls+=1
            if self.calls==1:
                loader=next(t['function'] for t in tools if t['function']['name']=='word.load_tools')
                assert 'create' in loader['parameters']['properties']['operations']['items']['enum']
                (store.root/'report.docx').write_bytes(b'Externally created after prompt')
                action={'name':'word.load_tools','arguments':{'operations':['create','set_section_layout']}}
            else:
                action={'name':'ask_user','arguments':{'text':'Scripted terminator'}}
            yield {'type':'token','text':'<tool_call>'+json.dumps(action)+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,RaceProvider(None))
    try:
        key=await runner.start('Create report.docx',artifact='report.docx',mode='background')
        await runner.job
        assert not store.db.execute('SELECT 1 FROM meta WHERE key=?',('word_tools:'+key,)).fetchone()
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        receipts=[json.loads(row[0]) for row in store.db.execute(
            "SELECT content FROM messages WHERE task_id=? AND role='tool'",(key,))]
        assert any('tool_state' in canonical(receipt) and 'Target already exists' in canonical(receipt) for receipt in receipts)
        assert not any('loaded_operations' in receipt for receipt in receipts)
    finally:await runner.pause();store.close()
