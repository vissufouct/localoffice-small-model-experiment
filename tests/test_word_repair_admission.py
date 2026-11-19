"""Probe reconstruction tests: SQLite copies only, no tokenizer/model/worker."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest

spec=importlib.util.spec_from_file_location('repair_admission_probe',Path(__file__).resolve().parents[1]/'scripts/probe_word_repair_admission.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


def fixture(tmp_path):
    root=tmp_path/'source';state=root/'.localoffice';state.mkdir(parents=True)
    (state/'blobs').mkdir();(state/'blobs'/'candidate').write_bytes(b'exact bytes')
    c=sqlite3.connect(state/'state.sqlite3')
    c.executescript('''
        CREATE TABLE messages(id INTEGER PRIMARY KEY,content TEXT);
        CREATE TABLE native_calls(call_id TEXT,task_id TEXT,assistant_message INTEGER,result_message INTEGER,function TEXT);
        CREATE TABLE operations(id TEXT,task_id TEXT,candidate_hash TEXT,path TEXT);
        CREATE TABLE candidates(path TEXT,operation_id TEXT,candidate_hash TEXT);
        CREATE TABLE message_chats(message_id INTEGER,chat_id TEXT);
        CREATE TABLE meta(key TEXT,value TEXT);
        CREATE TABLE tasks(id TEXT,status TEXT);
        CREATE TABLE task_chats(task_id TEXT,chat_id TEXT);
    ''')
    c.executemany('INSERT INTO messages VALUES(?,?)',[(1,'objective'),(2,'create'),(3,json.dumps({'status':'validated','receipt_id':'op','candidate_revision':'revision'})),(4,'load'),(5,'failed')])
    c.executemany('INSERT INTO native_calls VALUES(?,?,?,?,?)',[
        ('create-call','task',2,3,json.dumps({'name':'word.repair_creation','arguments':{}})),
        ('later-call','task',4,5,json.dumps({'name':'word.load_tools','arguments':{'operations':['replace_text']}}))])
    c.execute('INSERT INTO operations VALUES(?,?,?,?)',('op','task','revision','report.docx'))
    c.execute('INSERT INTO candidates VALUES(?,?,?)',('report.docx','op','revision'))
    c.executemany('INSERT INTO message_chats VALUES(?,?)',[(i,'chat') for i in range(1,6)])
    c.executemany('INSERT INTO meta VALUES(?,?)',[('task_recovery:task','later failure'),('history_floor:chat','4'),('keep','yes')])
    c.execute('INSERT INTO tasks VALUES(?,?)',('task','failed'));c.execute('INSERT INTO task_chats VALUES(?,?)',('task','chat'))
    c.commit();c.close()
    return root


def test_clone_cutoff_preserves_source_candidate_and_success(tmp_path):
    source=fixture(tmp_path);db=source/'.localoffice/state.sqlite3';before=probe.sha(db)
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as original:boundary=probe.first_creation(original,'task')
    assert boundary['cutoff']==3
    clone=tmp_path/'clone';probe.clone_state(source,clone)
    with sqlite3.connect(clone/'.localoffice/state.sqlite3') as c:
        probe.rewind(c,'task',boundary)
        assert c.execute('SELECT id FROM messages ORDER BY id').fetchall()==[(1,),(2,),(3,)]
        assert c.execute('SELECT call_id FROM native_calls').fetchall()==[('create-call',)]
        assert c.execute('SELECT key FROM meta').fetchall()==[('keep',)]
        assert c.execute('SELECT candidate_hash FROM candidates').fetchone()[0]=='revision'
    assert probe.sha(db)==before
    assert (clone/'.localoffice/blobs/candidate').read_bytes()==b'exact bytes'


def test_probe_refuses_cutoff_when_artifact_was_modified_later(tmp_path):
    source=fixture(tmp_path)
    with sqlite3.connect(source/'.localoffice/state.sqlite3') as c:
        boundary=probe.first_creation(c,'task')
        c.execute("UPDATE candidates SET candidate_hash='later'");c.commit()
        with pytest.raises(ValueError,match='Later artifact mutation'):probe.rewind(c,'task',boundary)
        assert c.execute('SELECT count(*) FROM messages').fetchone()[0]==5


def test_probe_refuses_success_receipt_without_exact_operation(tmp_path):
    source=fixture(tmp_path)
    with sqlite3.connect(source/'.localoffice/state.sqlite3') as c:
        c.execute("UPDATE operations SET candidate_hash='different'");c.commit()
        with pytest.raises(ValueError,match='matching operation'):probe.first_creation(c,'task')
