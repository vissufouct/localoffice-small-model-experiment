import json
import pytest
from localoffice.storage import ProjectStore
from localoffice.errors import HarnessError
from localoffice.word.content_plan import plan
from localoffice.word.content_coverage import occurrences,check,require_complete
from localoffice.word.creation_evidence import inspect
from test_creation_evidence import package,para,field
from test_word_progress import task


def setup(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    with store.transaction():
        store.db.execute("UPDATE tasks SET objective=?,artifact=? WHERE id='task'",('Exact body; Header; Page ; Cell','report.docx'))
    plan(store,'task',literals=[{'role':'paragraph','quote':'Exact body'},{'role':'header','quote':'Header'},
                               {'role':'footer','quote':'Page '},{'role':'table_cell','quote':'Cell'}])
    return store


def data(prefix='Page '):
    table='<w:tbl><w:tr><w:tc>'+para('Cell')+'</w:tc></w:tr></w:tbl>'
    footer=para(runs='<w:r><w:t xml:space="preserve">'+prefix+'</w:t></w:r>'
        '<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        '<w:r><w:t> of </w:t></w:r><w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>2</w:t></w:r></w:fldSimple>')
    return package(para('Exact body')+table,header=para('Header'),footer=footer)


def test_current_bytes_and_exact_role_segments_not_substrings_or_caches():
    found=occurrences(inspect(data()))
    assert found['paragraph']=={'Exact body'}
    assert found['table_cell']=={'Cell'}
    assert 'Page ' in found['footer']
    assert '1' not in found['footer'] and '2' not in found['footer']
    assert 'Page ' not in occurrences(inspect(data('Page of')))['footer']


def test_field_barriers_split_runs_and_complex_cache_never_matches():
    runs='<w:r><w:t>Pa</w:t></w:r><w:r><w:t>ge </w:t></w:r>'
    assert 'Page ' in occurrences(inspect(package(para('Body'),footer=para(runs=runs))))['footer']
    complex_story=para(runs='<w:r><w:fldChar w:fldCharType="begin"/></w:r>')+para('Page ')
    assert not occurrences(inspect(package(para('Body'),footer=complex_story)))['footer']
    assert not occurrences(inspect(package(para(runs=field('PAGE','Exact body')))))['paragraph']


def test_finish_checks_latest_bytes_and_never_changes_document(tmp_path):
    store=setup(tmp_path)
    try:
        path=tmp_path/'report.docx';path.write_bytes(data())
        good=require_complete(store,'task')
        assert len(good['matched'])==4 and good['pending']==[]
        path.write_bytes(data('Page of'))
        failed=check(store,'task')
        assert failed['revision']!=good['revision']
        assert [x['role'] for x in failed['pending']]==['footer']
        before=path.read_bytes()
        with pytest.raises(HarnessError,match='missing or unverified'):require_complete(store,'task')
        assert path.read_bytes()==before
    finally:store.close()


def test_no_plan_does_not_infer_obligations_from_old_reference_rows(tmp_path):
    store=ProjectStore(tmp_path);task(store)
    try:
        assert require_complete(store,'task') is None
    finally:store.close()


def test_candidate_overrides_saved_file_and_report_does_not_trust_binding_receipt(tmp_path):
    store=setup(tmp_path)
    try:
        (tmp_path/'report.docx').write_bytes(data())
        good=store.file_hash('report.docx')
        bad=store.put_blob(data('Page of'))
        # Coverage reads the same current candidate pointer as the UI, not old XML.
        with store.transaction():
            store.db.execute('INSERT INTO candidates VALUES(?,?,?,?)',('report.docx','synthetic',good,bad))
        report=check(store,'task')
        assert report['revision']==bad and report['pending'][0]['role']=='footer'
    finally:store.close()


@pytest.mark.parametrize("wrapper",["ins","del","moveTo","hyperlink","customXml"])
def test_unsupported_or_tracked_wrappers_never_establish_coverage(wrapper):
    wrapped=f'<w:{wrapper}><w:r><w:t>Exact body</w:t></w:r></w:{wrapper}>'
    assert not occurrences(inspect(package(para(runs=wrapped))))['paragraph']


@pytest.mark.asyncio
@pytest.mark.parametrize("correct",[False,True])
async def test_native_finish_cannot_bypass_active_plan(tmp_path,correct):
    from localoffice.runtime import TaskRunner
    from test_native_runner import NativeProvider
    store=ProjectStore(tmp_path)
    class Provider(NativeProvider):
        async def stream(self,messages,**kwargs):
            if self.calls==0:
                action={'tool':'word.plan_content','literals':[{'role':'paragraph','quote':'Exact body'},{'role':'footer','quote':'Page '}]}
            elif self.calls==1:action={'tool':'word.load_tools','operations':['create']}
            elif self.calls==2:
                action={'tool':'word.create','path':'report.docx','blocks':[{'kind':'paragraph','text':'Exact body'}],
                        'footer':{'text':'Page ' if correct else 'Page of','page_numbers':'current_total'}}
            else:
                names={t['function']['name'] for t in kwargs['tools']}
                assert ('finish' in names)==correct
                assert 'ask_user' in names and 'word.load_tools' in names and 'word.find_text' in names
                assert 'selected_content_check' in messages[0]['content']
                assert ('"status":"observed"' if correct else '"status":"pending"') in messages[0]['content']
                action={'tool':'finish','text':'Ready'}
            self.calls+=1
            yield {'type':'token','text':'<tool_call>'+json.dumps({'name':action.pop('tool'),'arguments':action})+'</tool_call>'}
            yield {'type':'finish','reason':'stop'}
    runner=TaskRunner(store,Provider(None),source_planning=True)
    try:
        key=await runner.start('Create report.docx with Exact body and footer Page followed by native fields.',artifact='report.docx',mode='background')
        await runner.job
        status=store.db.execute('SELECT status FROM tasks WHERE id=?',(key,)).fetchone()[0]
        if correct:
            assert status=='completed',runner.events
            report=json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',('word_content_coverage:'+key,)).fetchone()[0])
            assert report['pending']==[] and len(report['matched'])==2
            assert report['revision']==store.candidate('report.docx')['candidate_hash']
        else:
            assert status!='completed'
            assert any(e.get('code')=='native_tool_name' for e in runner.events)
            assert not any(e['type']=='completed' for e in runner.events)
            assert store.db.execute('SELECT value FROM meta WHERE key=?',('word_content_coverage:'+key,)).fetchone() is None
        assert not (tmp_path/'report.docx').exists()
        assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1
    finally:await runner.pause();store.close()


@pytest.mark.parametrize("part,mark",[("row","del"),("row","ins"),("cell","cellDel"),("cell","tcPrChange"),("table","tblPrChange")])
def test_tracked_table_structures_never_supply_literal_coverage(part,mark):
    rowprops=f'<w:trPr><w:{mark}/></w:trPr>' if part=="row" else ""
    cellprops=f'<w:tcPr><w:{mark}/></w:tcPr>' if part=="cell" else ""
    tableprops=f'<w:tblPr><w:{mark}/></w:tblPr>' if part=="table" else ""
    table=f'<w:tbl>{tableprops}<w:tr>{rowprops}<w:tc>{cellprops}{para("Cell")}</w:tc></w:tr></w:tbl>'
    assert not occurrences(inspect(package(table)))['table_cell']


def test_active_navigation_survives_restart_and_excludes_replaced_history(tmp_path):
    from localoffice.word.content_plan import active_navigation
    store=setup(tmp_path)
    try:
        plan(store,'task',plan_mode='replace',literals=[{'role':'paragraph','quote':'Exact body'}])
        store.close();store=ProjectStore(tmp_path)
        nav=active_navigation(store,'task')
        assert nav['count']==1 and nav['references'][0]['preview']=='Exact body'
        assert len(nav['references'])==1
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        assert active_navigation(store,'task')['references'][0]['status']=='stale'
        with pytest.raises(HarnessError):require_complete(store,'task')
    finally:store.close()


def test_feedback_is_bounded_current_and_non_mutating(tmp_path):
    from localoffice.word.content_coverage import feedback
    from localoffice.storage import canonical
    store=setup(tmp_path)
    try:
        path=tmp_path/'report.docx';path.write_bytes(data('Page of'))
        before=path.read_bytes();report=feedback(store,'task')
        assert report['status']=='pending' and report['matched']==3
        assert report['pending_count']==1 and report['pending'][0]['preview']=='Page '
        assert not report['pending'][0]['truncated']
        assert len(canonical(report).encode())<=640
        assert path.read_bytes()==before
        path.write_bytes(data())
        corrected=feedback(store,'task')
        assert corrected['status']=='observed' and corrected['pending_count']==0
        assert corrected['revision']!=report['revision']
        assert corrected['pending']==[]
    finally:store.close()


def test_feedback_reports_staleness_but_never_hides_corruption(tmp_path):
    from localoffice.word.content_coverage import feedback
    store=setup(tmp_path)
    try:
        (tmp_path/'report.docx').write_bytes(data())
        with store.transaction():store.db.execute("UPDATE tasks SET objective='Changed' WHERE id='task'")
        stale=feedback(store,'task')
        assert stale['status']=='unverified' and stale['plan_generation']==1
        assert stale['references'] and all('preview' not in x for x in stale['references'])
        assert stale['pending_count'] is None
        with pytest.raises(HarnessError):require_complete(store,'task')
        with store.transaction():
            store.db.execute("UPDATE meta SET value='{}' WHERE key LIKE 'word_active_literal_plan:%'")
        with pytest.raises(HarnessError) as exc:feedback(store,'task')
        assert exc.value.code=='state_corrupt'
    finally:store.close()


@pytest.mark.parametrize('budget',[256,640])
def test_feedback_unicode_byte_budget_and_explicit_omissions(tmp_path,budget):
    from localoffice.word.content_coverage import feedback
    from localoffice.storage import canonical
    store=ProjectStore(tmp_path);task(store)
    literal='🌍'*80
    try:
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective=?,artifact='report.docx' WHERE id='task'",(literal,))
        plan(store,'task',literals=[{'role':'paragraph','quote':literal}])
        (tmp_path/'report.docx').write_bytes(package(para('Unrelated')))
        result=feedback(store,'task',max_bytes=budget)
        assert len(canonical(result).encode())<=budget
        assert result['pending_count']==1 and result['status']=='pending'
        if budget==640:
            assert result['pending'][0]['ref']
            assert result['pending'][0]['truncated']
        else:assert result['truncated'] or (result['pending'] and result['pending'][0]['truncated'])
    finally:store.close()


def test_proactive_feedback_does_not_downgrade_invalid_package(tmp_path):
    from localoffice.word.content_coverage import feedback
    store=setup(tmp_path)
    try:
        (tmp_path/'report.docx').write_bytes(b'not a DOCX')
        with pytest.raises(HarnessError) as exc:feedback(store,'task')
        assert exc.value.code=='creation_evidence'
    finally:store.close()


def test_feedback_contrasts_expected_literal_with_exact_observed_segments(tmp_path):
    from localoffice.word.content_coverage import feedback
    store=setup(tmp_path)
    try:
        (tmp_path/'report.docx').write_bytes(data('Page of'))
        report=feedback(store,'task')
        pending=report['pending'][0]
        assert pending['preview']=='Page ' and pending['truncated'] is False
        assert 'Page of' in pending['observed_samples']
        assert not {'1','2'} & set(pending['observed_samples'])
        assert 'not missing fields' in report['scope']
    finally:store.close()


@pytest.mark.parametrize('kind',['header','footer'])
def test_only_enabled_explicit_variants_contribute_role_literals(kind):
    from test_creation_evidence import variant_package
    variants={(v,kind):para(v) for v in ('default','first','even')}
    inactive=occurrences(inspect(variant_package(variants=variants)))
    assert inactive[kind]=={'default'}
    first=occurrences(inspect(variant_package(variants=variants,title='<w:titlePg/>')))
    assert first[kind]=={'default','first'}
    even=occurrences(inspect(variant_package(variants=variants,even='<w:evenAndOddHeaders/>')))
    assert even[kind]=={'default','even'}
    both=occurrences(inspect(variant_package(variants=variants,title='<w:titlePg/>',even='<w:evenAndOddHeaders/>')))
    assert both[kind]=={'default','first','even'}
    assert not both['footer' if kind=='header' else 'header']


def test_activation_is_per_section_and_missing_refs_are_not_inherited():
    from test_creation_evidence import variant_package
    # Earlier enabled section has no explicit story; later explicit first story
    # is disabled. Never combine activation from one section with another's refs.
    earlier=para('',props='<w:sectPr><w:titlePg/></w:sectPr>')
    evidence=inspect(variant_package(variants={('first','footer'):para('Hidden')},extra_sections=earlier))
    assert len(evidence['sections'])==2
    assert evidence['sections'][0]['first_footer'] is None
    assert not occurrences(evidence)['footer']


@pytest.mark.parametrize('variant',['first','even'])
def test_active_variant_field_caches_and_unsafe_wrappers_do_not_match(variant):
    from test_creation_evidence import variant_package
    options={'title':'<w:titlePg/>','even':'<w:evenAndOddHeaders/>'}
    simple=para(runs='<w:r><w:t>Label </w:t></w:r><w:fldSimple w:instr="PAGE"><w:r><w:t>Cache</w:t></w:r></w:fldSimple>')
    found=occurrences(inspect(variant_package(variants={(variant,'footer'):simple},**options)))['footer']
    assert 'Label ' in found and 'Cache' not in found
    for unsafe in (para(runs=field('PAGE','Label ')),para(runs='<w:ins><w:r><w:t>Label </w:t></w:r></w:ins>')):
        assert not occurrences(inspect(variant_package(variants={(variant,'footer'):unsafe},**options)))['footer']


def test_finish_coverage_uses_current_variant_activation(tmp_path):
    from test_creation_evidence import variant_package
    store=ProjectStore(tmp_path);task(store)
    try:
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective=?,artifact=? WHERE id='task'",('First label','report.docx'))
        plan(store,'task',literals=[{'role':'footer','quote':'First label'}])
        path=tmp_path/'report.docx'
        path.write_bytes(variant_package(variants={('first','footer'):para('First label')}))
        with pytest.raises(HarnessError,match='missing or unverified'):require_complete(store,'task')
        path.write_bytes(variant_package(variants={('first','footer'):para('First label')},title='<w:titlePg/>'))
        assert require_complete(store,'task')['pending']==[]
    finally:store.close()


def test_feedback_prioritizes_pending_sources_over_optional_samples(tmp_path):
    from localoffice.word.content_coverage import feedback
    from localoffice.storage import canonical
    store=ProjectStore(tmp_path);task(store)
    try:
        with store.transaction():
            store.db.execute("UPDATE tasks SET objective=?,artifact='report.docx' WHERE id='task'",
                             ('First page; Second page',))
        selected=plan(store,'task',literals=[{'role':'paragraph','quote':'First page'},
                                          {'role':'paragraph','quote':'Second page'}])
        refs=[item['ref'] for item in selected['literals']]
        path=tmp_path/'report.docx'
        path.write_bytes(package(''.join(para('ref: '+ref) for ref in refs)))
        before=path.read_bytes()
        result=feedback(store,'task')
        assert len(canonical(result).encode())<=640
        assert result['pending_count']==2
        assert [item['ref'] for item in result['pending']]==refs
        assert [item['preview'] for item in result['pending']]==['First page','Second page']
        assert path.read_bytes()==before
        assert result['status']=='pending'
    finally:store.close()
