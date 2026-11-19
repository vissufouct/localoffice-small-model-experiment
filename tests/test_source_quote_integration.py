import json
import re
import pytest
from localoffice.storage import ProjectStore
from localoffice.word.source_quotes import candidates,select
from localoffice.native_history import record_call,reconcile_word_call
from localoffice.native_tools import source_quote_tools
from localoffice.decoding import native_tool_regex
from test_word_progress import task


def test_recovery_requires_exact_executed_call_and_forget_clears_receipt(tmp_path):
    from localoffice.memory import forget_project_memory
    store=ProjectStore(tmp_path);task(store)
    store.db.execute('UPDATE tasks SET objective=? WHERE id=?',('Write “Exact source”.','task'))
    projection=candidates(store,'task')
    args={'source_hash':projection['source_hash'],'selections':{'q01':'paragraph'}}
    identity=record_call(store,'task',{'tool':'word.select_source_quotes',**args})
    assert reconcile_word_call(store,'task')['state']=='unresolved'
    receipt=select(store,'task',**args,native_call_id=identity)
    assert receipt
    assert reconcile_word_call(store,'task')['state']=='reconciled'
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    another=record_call(store,'task',{'tool':'word.select_source_quotes',**args})
    assert another!=identity
    assert reconcile_word_call(store,'task')['state']=='unresolved'
    forget_project_memory(store,'FORGET PROJECT MEMORY')
    assert not store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'word_source_quote_selection:%'").fetchone()
    store.close()


def test_quote_grammar_masks_unknown_choices_roles_and_source_hash():
    tools=[{'type':'function','function':{'name':'word.select_source_quotes','parameters':{
        'type':'object','properties':{'source_hash':{'type':'string'},'selections':{'type':'object'}},
        'required':['source_hash','selections'],'additionalProperties':False}}},
        {'type':'function','function':{'name':'ask_user','parameters':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}}]
    projected=source_quote_tools(tools,{'available':True,'source_hash':'a'*64,'candidates':{'q01':{},'q02':{}}})
    regex=native_tool_regex(projected)
    def matches(selection,source='a'*64):
        return re.fullmatch(regex,'<tool_call>'+json.dumps({'name':'word.select_source_quotes','arguments':{'source_hash':source,'selections':selection}},separators=(',',':'))+'</tool_call>')
    assert matches({'q01':'paragraph'})
    assert matches({'q02':'footer'})
    assert not matches({'q03':'paragraph'})
    assert not matches({'q01':'invented'})
    assert not matches({'q01':'paragraph'},'b'*64)
    raw='<tool_call>'+json.dumps({'name':'word.select_source_quotes','arguments':{'source_hash':'a'*64,'selections':{'q01':'paragraph'}}},separators=(',',':'))+'</tool_call>'
    assert not re.fullmatch(regex,raw.replace('"q01":"paragraph"','"q01":"paragraph","q01":"footer"'))
    assert tools[0]['function']['parameters']['properties']['source_hash']=={'type':'string'}
