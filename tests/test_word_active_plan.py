"""Explicit active literal membership; no worker or model execution."""
import json
import pytest
from localoffice.storage import ProjectStore,canonical,digest
from localoffice.chats import current_chat,create_chat,select_chat
from localoffice.errors import HarnessError
from localoffice.word.content_plan import plan,active_plan,resolve,model_receipt,ACTIVE_PLAN_PREFIX


def setup(tmp_path,objective='Alpha Beta Gamma'):
    store=ProjectStore(tmp_path)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('task',objective,'running','background','report.docx',1,1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)',('task',current_chat(store)))
    return store


def select(store,quote,role='paragraph',**kwargs):
    return plan(store,'task',literals=[{'role':role,'quote':quote}],**kwargs)


def snapshot(store):
    return {table:[tuple(r) for r in store.db.execute('SELECT * FROM '+table)] for table in ('meta','word_content_refs','nodes','edges','operations')}


def test_no_active_record_does_not_activate_historical_references(tmp_path):
    store=setup(tmp_path)
    try:
        revision=digest(b'Alpha Beta Gamma')
        ref='ct_'+digest(canonical(['task','objective',revision,'paragraph',0,5]).encode())[:24]
        old={'ref':ref,'role':'paragraph','text':'Alpha','source':'objective','source_hash':revision,'start':0,'end':5}
        with store.transaction():store.db.execute('INSERT INTO word_content_refs VALUES(?,?,?)',(ref,'task',canonical(old)))
        assert active_plan(store,'task') is None
        result=select(store,'Beta')
        assert active_plan(store,'task')['refs']==[{'ref':result['literals'][0]['ref'],'role':'paragraph'}]
        assert resolve(store,'task',ref,role='paragraph')=='Alpha'
    finally:store.close()


def test_default_extend_is_stable_and_survives_restart(tmp_path):
    store=setup(tmp_path)
    try:
        first=select(store,'Alpha')
        assert first['active_plan']=={'version':1,'generation':1,'count':1}
        assert select(store,'Alpha')==first
        second=select(store,'Beta','header')
        state=active_plan(store,'task')
        assert state['generation']==2 and state['version']==1
        assert state['task_id']=='task' and state['chat_id']==current_chat(store) and state['target']=='report.docx'
        assert state['refs']==[{'ref':first['literals'][0]['ref'],'role':'paragraph'},{'ref':second['literals'][0]['ref'],'role':'header'}]
        store.close();store=ProjectStore(tmp_path)
        assert active_plan(store,'task')==state
        assert select(store,'Beta','header')==second
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


def test_replace_archives_membership_without_deleting_historical_refs(tmp_path):
    store=setup(tmp_path)
    try:
        first=select(store,'Alpha');prior=active_plan(store,'task')
        second=select(store,'Beta',plan_mode='replace')
        state=active_plan(store,'task')
        assert state['refs']==[{'ref':second['literals'][0]['ref'],'role':'paragraph'}]
        archived=store.db.execute('SELECT body,revision FROM nodes WHERE id=?',('literal-plan:task:1',)).fetchone()
        assert json.loads(archived['body'])==prior
        assert archived['revision']==digest(canonical(prior).encode())
        edge=store.db.execute("SELECT provenance FROM edges WHERE source='literal-plan:task:2' AND target='literal-plan:task:1' AND relation='supersedes-literal-plan'").fetchone()
        assert edge[0]=='explicit:replace'
        assert resolve(store,'task',first['literals'][0]['ref'],role='paragraph')=='Alpha'
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==2
        assert select(store,'Beta',plan_mode='replace')==second
    finally:store.close()


def test_failed_selection_never_changes_active_state_or_persistence(tmp_path):
    store=setup(tmp_path)
    try:
        select(store,'Alpha');before=snapshot(store)
        with pytest.raises(HarnessError):
            plan(store,'task',plan_mode='replace',literals=[{'role':'paragraph','quote':'Beta'},{'role':'footer','quote':'Missing'}])
        assert snapshot(store)==before
    finally:store.close()


def test_archive_failure_rolls_back_new_refs_and_active_update(tmp_path,monkeypatch):
    store=setup(tmp_path)
    try:
        select(store,'Alpha');before=snapshot(store);original=store.node
        def failing_node(key,*args,**kwargs):
            if key.startswith('literal-plan:'):raise RuntimeError('simulated persistence failure')
            return original(key,*args,**kwargs)
        monkeypatch.setattr(store,'node',failing_node)
        with pytest.raises(RuntimeError):select(store,'Beta',plan_mode='replace')
        assert snapshot(store)==before
    finally:store.close()


@pytest.mark.parametrize('origin',['objective','message'])
def test_only_explicit_replace_recovers_stale_source(origin,tmp_path):
    store=setup(tmp_path)
    try:
        kwargs={}
        if origin=='message':kwargs['source_message']=store.message('user','Alpha Beta','task')
        first=select(store,'Alpha',**kwargs)
        with store.transaction():
            if origin=='objective':store.db.execute("UPDATE tasks SET objective='New Alpha Beta' WHERE id='task'")
            else:store.db.execute('UPDATE messages SET content=? WHERE id=?',('New Alpha Beta',kwargs['source_message']))
        before=snapshot(store)
        with pytest.raises(HarnessError):active_plan(store,'task')
        with pytest.raises(HarnessError):select(store,'Beta',**kwargs)
        assert snapshot(store)==before
        replacement=select(store,'Beta',plan_mode='replace',**kwargs)
        assert replacement['active_plan']['generation']==2
        assert active_plan(store,'task')['refs']==[{'ref':replacement['literals'][0]['ref'],'role':'paragraph'}]
        assert store.db.execute('SELECT 1 FROM word_content_refs WHERE ref=?',(first['literals'][0]['ref'],)).fetchone()
    finally:store.close()


@pytest.mark.parametrize('change',['json','hash','reference','missing_reference'])
def test_corruption_cannot_be_repaired_by_replace(tmp_path,change):
    store=setup(tmp_path)
    try:
        first=select(store,'Alpha');ref=first['literals'][0]['ref']
        with store.transaction():
            if change=='json':store.db.execute('UPDATE meta SET value=? WHERE key=?',('{',ACTIVE_PLAN_PREFIX+'task'))
            elif change=='hash':
                row=store.db.execute('SELECT value FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',)).fetchone()
                envelope=json.loads(row[0]);envelope['state']['generation']=99
                store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),ACTIVE_PLAN_PREFIX+'task'))
            elif change=='reference':store.db.execute('UPDATE word_content_refs SET body=? WHERE ref=?',('{}',ref))
            else:store.db.execute('DELETE FROM word_content_refs WHERE ref=?',(ref,))
        before=snapshot(store)
        for operation in (lambda:active_plan(store,'task'),lambda:select(store,'Beta',plan_mode='replace')):
            with pytest.raises(HarnessError) as error:operation()
            assert error.value.code=='state_corrupt'
        assert snapshot(store)==before
    finally:store.close()


@pytest.mark.parametrize('change',['chat','target','copied_task'])
def test_active_plan_owner_and_target_fail_closed(tmp_path,change):
    store=setup(tmp_path)
    try:
        select(store,'Alpha');owner='task'
        if change=='chat':select_chat(store,create_chat(store))
        elif change=='target':
            with store.transaction():store.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
        else:
            with store.transaction():
                store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('other','Alpha Beta Gamma','running','background','report.docx',1,1))
                store.db.execute('INSERT INTO task_chats VALUES(?,?)',('other',current_chat(store)))
                value=store.db.execute('SELECT value FROM meta WHERE key=?',(ACTIVE_PLAN_PREFIX+'task',)).fetchone()[0]
                store.db.execute('INSERT INTO meta VALUES(?,?)',(ACTIVE_PLAN_PREFIX+'other',value))
            owner='other'
        with pytest.raises(HarnessError):active_plan(store,owner)
        with pytest.raises(HarnessError):plan(store,owner,plan_mode='replace',literals=[{'role':'paragraph','quote':'Beta'}])
    finally:store.close()


def test_512_active_cap_and_historical_retention_after_replacement(tmp_path):
    labels=[f'[{i:03d}]' for i in range(513)]
    store=setup(tmp_path,' '.join(labels))
    try:
        for start in range(0,512,24):
            plan(store,'task',literals=[{'role':'paragraph','quote':x} for x in labels[start:min(start+24,512)]])
        assert len(active_plan(store,'task')['refs'])==512
        before=snapshot(store)
        with pytest.raises(HarnessError,match='512'):select(store,labels[512])
        assert snapshot(store)==before
        with pytest.raises(HarnessError,match='512'):select(store,labels[512],plan_mode='replace')
        assert snapshot(store)==before
        prior=active_plan(store,'task')
        result=select(store,labels[0],plan_mode='replace')
        assert result['active_plan']['count']==1
        assert store.db.execute('SELECT count(*) FROM word_content_refs').fetchone()[0]==512
        archived=store.db.execute('SELECT body FROM nodes WHERE id=?',(f'literal-plan:task:{prior["generation"]}',)).fetchone()
        assert json.loads(archived[0])==prior
        for item in prior['refs']:
            assert store.db.execute('SELECT 1 FROM word_content_refs WHERE ref=?',(item['ref'],)).fetchone()
        after=snapshot(store)
        # A smaller active plan does not free historical storage capacity.
        for mode in ('extend','replace'):
            with pytest.raises(HarnessError,match='512'):select(store,labels[512],plan_mode=mode)
        assert snapshot(store)==after
    finally:store.close()


def test_model_receipt_keeps_concise_active_metadata_and_obligation(tmp_path):
    store=setup(tmp_path)
    try:
        first=select(store,'Alpha');second=select(store,'Beta')
        projected=model_receipt(second)
        assert projected['active_plan']=={'version':1,'generation':2,'count':2}
        assert projected['active_plan']==second['active_plan']
        assert 'must be retained before completion' in projected['binding']
        assert 'reference_hashes' not in projected
        assert model_receipt(projected)==projected
        assert first['literals'][0]['ref'] in {x['ref'] for x in active_plan(store,'task')['refs']}
    finally:store.close()
