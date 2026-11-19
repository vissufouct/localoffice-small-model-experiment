import json
import pytest
from localoffice.storage import ProjectStore,canonical
from localoffice.chats import current_chat,create_chat,select_chat
from localoffice.errors import HarnessError
from localoffice.word import creation_repair as repair
from localoffice.word.content_plan import plan


def setup(tmp_path):
    store=ProjectStore(tmp_path)
    with store.transaction():
        store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('task','Exact body','running','background','report.docx',1,1))
        store.db.execute('INSERT INTO task_chats VALUES(?,?)',('task',current_chat(store)))
    ref=plan(store,'task',literals=[{'role':'paragraph','quote':'Exact body'}])['literals'][0]['ref']
    payload={'path':'report.docx','blocks':[{'kind':'paragraph','text':''}],
             'header':{'text':'Header','style':'Band'},'footer':{'text':'Page ','style':'Band','page_numbers':'current_total'},
             'literal_refs':{'blocks.0.text':ref}}
    return store,payload


def test_capture_restart_resolve_preserves_exact_payload_and_pending(tmp_path):
    store,payload=setup(tmp_path)
    try:
        original=canonical(payload)
        receipt=repair.capture(store,'task',payload)
        assert receipt['missing_styles']==[{'id':'Band','slots':['header.style','footer.style']}]
        assert 'payload' not in receipt and len(canonical(receipt))<1000
        assert repair.capture(store,'task',payload)==receipt
        assert canonical(payload)==original
        assert repair.get(store,'task')['payload']==payload
        store.close();store=ProjectStore(tmp_path)
        request=repair.resolve(store,'task',receipt['ref'],[{'id':'Band','font':'Calibri','size_points':10}])
        assert request.literal_refs==payload['literal_refs']
        assert request.blocks[0].text=='' and request.footer.page_numbers=='current_total'
        assert request.styles[0].font=='Calibri'
        assert repair.pending(store,'task')==receipt
        assert repair.get(store,'task')['payload']==payload
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert not (tmp_path/'report.docx').exists()
    finally:store.close()


@pytest.mark.parametrize('change',['unknown_field','duplicate_style','bad_break','bad_xml','valid','too_many_styles','bad_literal'])
def test_capture_abstains_for_non_missing_style_failures(tmp_path,change):
    store,payload=setup(tmp_path)
    try:
        if change=='unknown_field':payload['surprise']=True
        elif change=='duplicate_style':payload['styles']=[{'id':'Existing'},{'id':'Existing'}]
        elif change=='bad_break':payload['blocks'].append({'kind':'page_break'})
        elif change=='bad_xml':payload['blocks'][0]['text']='\x01'
        elif change=='valid':payload['styles']=[{'id':'Band'}]
        elif change=='too_many_styles':payload['styles']=[{'id':f'Style{i}'} for i in range(16)]
        else:payload['literal_refs']['blocks.0.text']='ct_'+'0'*24
        assert repair.capture(store,'task',payload) is None
        assert repair.get(store,'task') is None
    finally:store.close()


@pytest.mark.parametrize('change',['objective','artifact','file','candidate','chat','source','tamper','ref'])
def test_repair_rejects_stale_tampered_or_wrong_owner(tmp_path,change):
    store,payload=setup(tmp_path)
    try:
        receipt=repair.capture(store,'task',payload)
        ref=receipt['ref']
        if change=='objective':
            with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        elif change=='artifact':
            with store.transaction():store.db.execute("UPDATE tasks SET artifact='other.docx' WHERE id='task'")
        elif change=='file':(tmp_path/'report.docx').write_bytes(b'external')
        elif change=='candidate':
            with store.transaction():store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',('report.docx','operation',None,'0'*64))
        elif change=='chat':select_chat(store,create_chat(store))
        elif change=='source':store.message('user','Revised instruction','task')
        elif change=='ref':ref='cr_'+'0'*32
        else:
            row=store.db.execute('SELECT value FROM meta WHERE key=?',(repair.PREFIX+'task',)).fetchone()
            envelope=json.loads(row[0]);envelope['state']['payload']['header']['text']='Tampered'
            with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',(canonical(envelope),repair.PREFIX+'task'))
        with pytest.raises(HarnessError):repair.resolve(store,'task',ref,[{'id':'Band','font':'Calibri'}])
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()


@pytest.mark.parametrize('styles',[
    [],[{'id':'Other'}],[{'id':'Band'},{'id':'Band'}],
    [{'id':'Band'},{'id':'Normal'}],[{'id':'Band'},{'id':'Existing'}],
])
def test_only_explicit_missing_style_ids_can_be_added(tmp_path,styles):
    store,payload=setup(tmp_path)
    payload['styles']=[{'id':'Existing','font':'Courier New'}]
    try:
        receipt=repair.capture(store,'task',payload)
        with pytest.raises(HarnessError):repair.resolve(store,'task',receipt['ref'],styles)
        assert repair.get(store,'task')['payload']==payload
        assert repair.pending(store,'task')==receipt
    finally:store.close()


def test_new_proposal_supersedes_old_handle_and_get_remains_available_after_staging(tmp_path):
    store,payload=setup(tmp_path)
    try:
        first=repair.capture(store,'task',payload)
        payload['header']['text']='Explicit revision'
        second=repair.capture(store,'task',payload)
        assert first['ref']!=second['ref']
        with pytest.raises(HarnessError):repair.resolve(store,'task',first['ref'],[{'id':'Band'}])
        repair.resolve(store,'task',second['ref'],[{'id':'Band'}])
        (tmp_path/'report.docx').write_bytes(b'staged elsewhere')
        assert repair.get(store,'task',second['ref'])['resolved_request']['header']['text']=='Explicit revision'
        stale=repair.pending(store,'task')
        assert stale['status']=='stale' and stale['new_proposal_needed'] is True
    finally:store.close()


@pytest.mark.parametrize('change',['answer','objective'])
def test_stale_pending_allows_explicit_same_payload_resubmission(tmp_path,change):
    store,payload=setup(tmp_path)
    try:
        # Unbound text permits an unchanged proposal after an objective update;
        # source-bound old references must instead be replanned.
        if change=='objective':
            payload.pop('literal_refs');payload['blocks'][0]['text']='Exact body'
        first=repair.capture(store,'task',payload)
        if change=='answer':store.message('user','Use Calibri for the missing style.','task')
        else:
            with store.transaction():store.db.execute("UPDATE tasks SET objective='Exact body with Calibri' WHERE id='task'")
        before=repair.get(store,'task')
        stale=repair.pending(store,'task')
        assert stale['status']=='stale' and stale['new_proposal_needed'] is True
        assert stale['ref']==first['ref'] and 'payload' not in stale
        assert repair.get(store,'task')==before  # Navigation cannot recapture.
        with pytest.raises(HarnessError):repair.resolve(store,'task',first['ref'],[{'id':'Band'}])
        second=repair.capture(store,'task',payload)
        assert second['status']=='missing_styles' and second['ref']!=first['ref']
        assert repair.get(store,'task')['payload_hash']==before['payload_hash']
        assert repair.pending(store,'task')==second
        with pytest.raises(HarnessError):repair.resolve(store,'task',first['ref'],[{'id':'Band'}])
        assert repair.resolve(store,'task',second['ref'],[{'id':'Band','font':'Calibri'}]).styles[0].font=='Calibri'
    finally:store.close()


def test_stale_literal_refs_require_replanning_before_recapture(tmp_path):
    store,payload=setup(tmp_path)
    try:
        first=repair.capture(store,'task',payload)
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Keep Exact body' WHERE id='task'")
        assert repair.pending(store,'task')['status']=='stale'
        assert repair.capture(store,'task',payload) is None
        assert repair.get(store,'task')['ref']==first['ref']
        new_literal=plan(store,'task',plan_mode='replace',literals=[{'role':'paragraph','quote':'Exact body'}])['literals'][0]['ref']
        payload['literal_refs']['blocks.0.text']=new_literal
        second=repair.capture(store,'task',payload)
        assert second['ref']!=first['ref']
        assert repair.resolve(store,'task',second['ref'],[{'id':'Band'}]).literal_refs==payload['literal_refs']
    finally:store.close()


@pytest.mark.parametrize('change',['corrupt_record','corrupt_literal','wrong_chat'])
def test_pending_and_recapture_never_soften_corruption_or_ownership(tmp_path,change):
    store,payload=setup(tmp_path)
    try:
        repair.capture(store,'task',payload)
        if change=='wrong_chat':select_chat(store,create_chat(store))
        elif change=='corrupt_record':
            with store.transaction():store.db.execute('UPDATE meta SET value=? WHERE key=?',('{',repair.PREFIX+'task'))
        else:
            with store.transaction():store.db.execute('UPDATE word_content_refs SET body=? WHERE ref=?',('{',payload['literal_refs']['blocks.0.text']))
        for operation in (lambda:repair.pending(store,'task'),lambda:repair.capture(store,'task',payload)):
            with pytest.raises(HarnessError) as error:operation()
            assert error.value.code in ('state_corrupt','creation_repair_scope','chat_scope')
    finally:store.close()


def test_missing_literal_reference_is_stale_navigation_not_repair_authority(tmp_path):
    store,payload=setup(tmp_path)
    try:
        first=repair.capture(store,'task',payload)
        with store.transaction():store.db.execute('DELETE FROM word_content_refs')
        stale=repair.pending(store,'task')
        assert stale['status']=='stale' and stale['reason']=='content_reference'
        with pytest.raises(HarnessError):repair.resolve(store,'task',first['ref'],[{'id':'Band'}])
        assert repair.capture(store,'task',payload) is None
    finally:store.close()


@pytest.mark.parametrize('slot',['first_header','first_footer','even_header','even_footer'])
def test_missing_variant_style_repair_preserves_band_content(tmp_path,slot):
    store,payload=setup(tmp_path)
    try:
        payload.pop('header');payload.pop('footer')
        payload[slot]={'text':'Exact variant','style':'Variant','alignment':'right','page_numbers':'current_total'}
        receipt=repair.capture(store,'task',payload)
        assert receipt['missing_styles']==[{'id':'Variant','slots':[slot+'.style']}]
        request=repair.resolve(store,'task',receipt['ref'],[{'id':'Variant','font':'Arial','size_points':9}])
        band=getattr(request,slot)
        assert band.text=='Exact variant' and band.alignment=='right' and band.page_numbers=='current_total'
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
    finally:store.close()
