"""Typed creation bindings preserve caller identity and resolver authority."""
import io
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError
from localoffice.errors import HarnessError
from localoffice.storage import ProjectStore, canonical, digest
from localoffice.word.contracts import CreationSourceBinding, RepairCreation, FinalizeCreation
from localoffice.word.content_plan import plan
from localoffice.word.creation_repair import capture
from localoffice.word.plugin import WordPlugin
from test_word_progress import task
from test_word_creation import W


@pytest.fixture
def setup(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective=? WHERE id='task'",('Exact body; Exact header; Page ; Cell literal; Alternate footer',))
    refs=plan(store,'task',literals=[{'role':r,'quote':q} for r,q in [
        ('paragraph','Exact body'),('header','Exact header'),('footer','Page '),
        ('table_cell','Cell literal'),('footer','Alternate footer')]])['literals']
    request={'tool':'word.create','path':'report.docx','blocks':[
        {'kind':'paragraph','text':''}, {'kind':'table','rows':[['']]}]}
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        request[name]={'text':'','style':'Band','alignment':'center','page_numbers':'current_total','separator':' of '}
    try:yield store,request,{item['role']:item['ref'] for item in refs[:4]},refs[-1]['ref']
    finally:store.close()


def retained(store,request,mode):
    if mode=='finalize_creation':
        request={**request,'styles':[{'id':'Band','size_points':9}]}
        result=WordPlugin(store).prepare_creation(task_id='task',**{k:v for k,v in request.items() if k!='tool'})
        return result['ref'],[]
    return capture(store,'task',request)['ref'],[{'id':'Band','size_points':9}]


def execute(store,mode,ref,styles,**kwargs):
    return getattr(WordPlugin(store),mode)(task_id='task',idempotency_key='typed',ref=ref,styles=styles,**kwargs)


def slot_value(request,slot):
    value=request
    for part in slot.split('.'):
        value=value[int(part)] if isinstance(value,list) else value[part]
    return value


@pytest.mark.parametrize('mode',['repair_creation','finalize_creation'])
def test_real_worker_all_slots_exact_provenance_and_original_representation(setup,mode):
    store,request,refs,_=setup
    ref,styles=retained(store,request,mode)
    bindings=[{'slot':'blocks.0.text','ref':refs['paragraph']},{'slot':'blocks.1.rows.0.0','ref':refs['table_cell']}]
    for name in ('header','footer','first_header','first_footer','even_header','even_footer'):
        bindings.append({'slot':name+'.text','ref':refs[name.rsplit('_',1)[-1]]})
    result=execute(store,mode,ref,styles,bindings=bindings)
    saved=json.loads(store.operation(result['receipt_id'])['args'])
    assert saved['repair_request']['bindings']==bindings
    assert saved['repair_request']['source_bindings']=={}
    assert saved['repair_origin']['payload']['footer']['text']==''
    expected={'paragraph':'Exact body','table_cell':'Cell literal','header':'Exact header','footer':'Page '}
    for item in bindings:
        provenance=next(b for b in saved['literal_bindings'] if b['slot']==item['slot'])
        assert provenance['ref']==item['ref']
        assert slot_value(saved['request'],item['slot'])==expected[provenance['role']]
        original=json.loads(store.db.execute('SELECT body FROM word_content_refs WHERE ref=?',(item['ref'],)).fetchone()[0])
        assert provenance=={**original,'slot':item['slot']}
    with zipfile.ZipFile(io.BytesIO(store.blob(result['candidate_revision']))) as package:
        doc=ET.fromstring(package.read('word/document.xml'))
        assert doc.find('.//{'+W+'}t').text=='Exact body'
        assert 'Cell literal' in ''.join(doc.itertext())
        bands=[ET.fromstring(package.read(n)) for n in package.namelist() if n.endswith('.xml')]
        bands=[b for b in bands if b.tag in ('{'+W+'}hdr','{'+W+'}ftr')]
        assert len(bands)==6
        for b in bands:
            assert b.find('.//{'+W+'}t').text==('Page ' if b.tag.endswith('ftr') else 'Exact header')
            assert [f.get('{'+W+'}instr').strip() for f in b.findall('.//{'+W+'}fldSimple')]==['PAGE','NUMPAGES']
    assert not (store.root/'report.docx').exists()


@pytest.mark.parametrize('kind',['role','slot','inactive','stale'])
def test_typed_bindings_enforce_resolver_checks_without_staging(setup,kind):
    store,request,refs,alternate=setup
    ref,styles=retained(store,request,'finalize_creation')
    bindings=[{'slot':'footer.text','ref':refs['footer']}]
    if kind=='role':bindings[0]['ref']=refs['header']
    elif kind=='slot':bindings[0]['slot']='blocks.99.text'
    elif kind=='inactive':plan(store,'task',plan_mode='replace',literals=[{'role':'footer','quote':'Alternate footer'}])
    else:
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed source' WHERE id='task'")
    with pytest.raises(HarnessError):execute(store,'finalize_creation',ref,styles,bindings=bindings)
    assert not store.candidate('report.docx')
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0


@pytest.mark.parametrize('model',[RepairCreation,FinalizeCreation])
@pytest.mark.parametrize('bad',['duplicate','mixed','too_many','extra','empty_slot','bad_ref'])
def test_closed_bounded_nonambiguous_contract(model,bad):
    item={'slot':'footer.text','ref':'ct_'+'a'*24};args={'ref':'cr_'+'b'*32,'styles':[{'id':'Band'}],'bindings':[item]}
    if bad=='duplicate':args['bindings']=[item,item]
    elif bad=='mixed':args['source_bindings']={'footer.text':item['ref']}
    elif bad=='too_many':args['bindings']=[{**item,'slot':f'blocks.{i}.text'} for i in range(25)]
    elif bad=='extra':args['bindings']=[{**item,'text':'Page '}]
    elif bad=='empty_slot':args['bindings']=[{**item,'slot':''}]
    else:args['bindings']=[{**item,'ref':'not-a-ref'}]
    with pytest.raises(ValidationError):model(**args)
    assert CreationSourceBinding(**item).model_dump()==item


def test_replay_retains_list_order_and_representation_after_plan_replace(setup,monkeypatch):
    store,request,refs,_=setup;ref,styles=retained(store,request,'finalize_creation')
    bindings=[{'slot':'footer.text','ref':refs['footer']},{'slot':'header.text','ref':refs['header']}]
    result=execute(store,'finalize_creation',ref,styles,bindings=bindings)
    before=dict(store.operation(result['receipt_id']))
    plan(store,'task',plan_mode='replace',literals=[{'role':'footer','quote':'Alternate footer'}])
    def forbidden(*args,**kwargs):pytest.fail('Replay must not resolve or execute')
    monkeypatch.setattr(WordPlugin,'_execute',forbidden)
    monkeypatch.setattr('localoffice.word.creation_repair.resolve',forbidden)
    assert execute(store,'finalize_creation',ref,styles,bindings=bindings)==result
    for changed in ({'bindings':list(reversed(bindings))},{'source_bindings':{b['slot']:b['ref'] for b in bindings}}):
        with pytest.raises(HarnessError) as exc:execute(store,'finalize_creation',ref,styles,**changed)
        assert exc.value.code=='idempotency_conflict'
    assert dict(store.operation(result['receipt_id']))==before


@pytest.mark.parametrize('tamper',[False,True])
def test_legacy_map_compatibility_verifies_original_hash_before_empty_list_normalization(setup,monkeypatch,tamper):
    store,request,refs,_=setup;ref,styles=retained(store,request,'repair_creation')
    legacy={'footer.text':refs['footer']}
    result=execute(store,'repair_creation',ref,styles,source_bindings=legacy)
    row=dict(store.operation(result['receipt_id']));saved=json.loads(row['args'])
    assert saved['repair_request'].pop('bindings')==[]
    assert saved['repair_request']['source_bindings']==legacy
    encoded=canonical(saved)
    with store.transaction():store.db.execute('UPDATE operations SET args=?,args_hash=? WHERE id=?',
        (encoded,row['args_hash'] if tamper else digest(encoded.encode()),result['receipt_id']))
    before=dict(store.operation(result['receipt_id']))
    monkeypatch.setattr(WordPlugin,'_execute',lambda *a,**k:pytest.fail('No worker on replay'))
    if tamper:
        with pytest.raises(HarnessError) as exc:execute(store,'repair_creation',ref,styles,source_bindings=legacy)
        assert exc.value.code=='state_corrupt'
    else:assert execute(store,'repair_creation',ref,styles,source_bindings=legacy)==result
    assert dict(store.operation(result['receipt_id']))==before
