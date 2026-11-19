"""Complete compact source choices without guessed slot assignments."""
import pytest
from localoffice.errors import HarnessError
from localoffice.storage import canonical
from localoffice.word.content_plan import plan,binding_navigation
from test_word_content_plan import fixture


def test_ten_variant_and_body_references_fit_without_losing_exact_spaces(tmp_path):
    entries=[{'role':'paragraph','quote':f'Body page {n}'} for n in range(1,5)]
    entries += [{'role':role,'quote':label+suffix} for label in ('First','Even','Default')
                for role,suffix in [('header',' header'),('footer',' page ')]]
    store=fixture(tmp_path,';'.join(e['quote'] for e in entries))
    try:
        receipt=plan(store,'task',literals=entries)
        before=list(store.db.iterdump())
        result=binding_navigation(store,'task')
        assert result['count']==10 and result['truncated'] is False
        assert len(canonical(result).encode())<=1200
        for item in receipt['literals']:
            assert result['by_role'][item['role']][item['ref']]==item['text']
        assert list(store.db.iterdump())==before
    finally:store.close()


def test_long_unicode_previews_are_explicitly_truncated_and_byte_bounded(tmp_path):
    text='漢'*100
    store=fixture(tmp_path,text)
    try:
        ref=plan(store,'task',literals=[{'role':'paragraph','quote':text}])['literals'][0]['ref']
        result=binding_navigation(store,'task')
        assert result['by_role']['paragraph'][ref]=={'preview':text[:80],'truncated':True}
        tiny=binding_navigation(store,'task',max_bytes=100)
        assert tiny['truncated'] and not tiny['by_role']
        assert len(canonical(tiny).encode())<=100
    finally:store.close()


def test_replaced_membership_does_not_expose_historical_references(tmp_path):
    store=fixture(tmp_path,'Old. New.')
    try:
        old=plan(store,'task',literals=[{'role':'paragraph','quote':'Old.'}])['literals'][0]['ref']
        new=plan(store,'task',literals=[{'role':'paragraph','quote':'New.'}],plan_mode='replace')['literals'][0]['ref']
        result=binding_navigation(store,'task')
        assert result['by_role']=={'paragraph':{new:'New.'}}
        assert old not in canonical(result)
        store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        stale=binding_navigation(store,'task')
        assert not stale['by_role']
        assert stale['status']=='plan_repair_required'
    finally:store.close()


@pytest.mark.parametrize('count,max_bytes',[(1,1200),(24,1200),(24,100)])
def test_any_stale_member_hides_all_choices_even_beyond_display_limits(tmp_path,count,max_bytes):
    entries=[{'role':'paragraph','quote':f'Fresh {n}!'} for n in range(count)]
    store=fixture(tmp_path,';'.join(item['quote'] for item in entries))
    try:
        plan(store,'task',literals=entries)
        message=store.message('user','Old footer','task')
        plan(store,'task',source_message=message,literals=[{'role':'footer','quote':'Old footer'}])
        store.db.execute('UPDATE messages SET content=? WHERE id=?',('Changed footer',message))
        before=list(store.db.iterdump())
        result=binding_navigation(store,'task',max_bytes=max_bytes)
        assert result=={'count':count+1,'by_role':{},'truncated':True,'status':'plan_repair_required'}
        assert len(canonical(result).encode())<=max_bytes
        assert list(store.db.iterdump())==before
        # Only an explicit plan replacement restores offered choices.
        plan(store,'task',plan_mode='replace',literals=[entries[0]])
        repaired=binding_navigation(store,'task')
        assert repaired['by_role'] and 'status' not in repaired
    finally:store.close()


def test_corrupt_member_is_not_downgraded_to_plan_repair(tmp_path):
    store=fixture(tmp_path,'Body')
    try:
        plan(store,'task',literals=[{'role':'paragraph','quote':'Body'}])
        store.db.execute("UPDATE word_content_refs SET body='{}'")
        with pytest.raises(HarnessError) as error:binding_navigation(store,'task')
        assert error.value.code=='state_corrupt'
    finally:store.close()


def test_navigation_hard_byte_cap_and_minimum_budget(tmp_path):
    entries=[{'role':'paragraph','quote':f'{n}'+('漢'*79)} for n in range(24)]
    store=fixture(tmp_path,';'.join(item['quote'] for item in entries))
    try:
        plan(store,'task',literals=entries)
        result=binding_navigation(store,'task',max_bytes=10000)
        assert len(canonical(result).encode())<=1200 and result['truncated']
        with pytest.raises(HarnessError):binding_navigation(store,'task',max_bytes=50)
    finally:store.close()
