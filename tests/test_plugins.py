import pytest
from localoffice.storage import ProjectStore
from localoffice.plugins import first_party_registry
from localoffice.skills import SkillLoader
from localoffice.errors import HarnessError


def test_disabled_plugin_cannot_mutate(tmp_path):
    store=ProjectStore(tmp_path);registry=first_party_registry(store)
    registry.set_enabled('artifact-markdown',False)
    with pytest.raises(HarnessError,match='disabled'):
        registry.call('stage_markdown',path='x.md',text='x',expected_revision=None,idempotency_key='x')
    assert store.candidate('x.md') is None
    assert not (tmp_path/'x.md').exists()
    with pytest.raises(HarnessError):registry.call('apply',operation_id='x')
    store.close()


def test_skill_import_does_not_execute_scripts(tmp_path):
    project=tmp_path/'project';project.mkdir()
    source=tmp_path/'skill';source.mkdir();(source/'scripts').mkdir()
    (source/'SKILL.md').write_text('---\nname: portable\ndescription: A portable skill\nallowed-tools: read_file\n---\nRead evidence before drafting.')
    (source/'scripts'/'run.py').write_text("raise RuntimeError('must never run')")
    store=ProjectStore(project);loader=SkillLoader(store)
    item=loader.import_local(source)
    assert item['status']=='ready'
    assert item['scripts_execution']=='disabled'
    assert loader.load(item['id'])['body']=='Read evidence before drafting.'
    store.close()


def test_host_specific_skill_requires_adaptation(tmp_path):
    store=ProjectStore(tmp_path)
    folder=tmp_path/'.claude/skills/example';folder.mkdir(parents=True)
    (folder/'SKILL.md').write_text('---\nname: incompatible\nallowed-tools: Bash\n---\nRun shell commands.')
    loader=SkillLoader(store);items=loader.discover()
    item=next(i for i in items if i.get('name')=='incompatible')
    assert item['status']=='adaptation needed'
    with pytest.raises(HarnessError,match='adaptation'):loader.load(item['id'])
    store.close()


@pytest.mark.asyncio
async def test_selected_skill_is_pinned_and_survives_source_change(tmp_path):
    from localoffice.runtime import TaskRunner
    class Provider:
        context_limit=4096
        def status(self):return {'loaded':True}
        async def count_tokens(self,messages):return 300
        async def stream(self,messages,**kwargs):
            yield {'type':'token','text':'{"tool":"finish","text":"Done"}'}
            yield {'type':'finish','reason':'stop'}
    folder=tmp_path/'.agents/skills/concise';folder.mkdir(parents=True)
    source=folder/'SKILL.md'
    source.write_text('---\nname: concise\nallowed-tools: read_file\n---\nPreserve source citations in every summary.')
    store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    item=next(s for s in runner.skills.discover() if s.get('name')=='concise')
    task=await runner.start('Summarise',skill_id=item['id']);await runner.job
    source.write_text('Changed instructions must not leak into the existing task.')
    assert 'Preserve source citations' in (await runner.assemble(task))[0]['content']
    assert 'Changed instructions' not in (await runner.assemble(task))[0]['content']
    store.close();store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    assert 'Preserve source citations' in (await runner.assemble(task))[0]['content']
    store.close()


@pytest.mark.asyncio
async def test_skill_dependency_requires_enabled_plugin(tmp_path):
    from localoffice.runtime import TaskRunner
    class Provider:
        def status(self):return {'loaded':True}
    folder=tmp_path/'.agents/skills/reader';folder.mkdir(parents=True)
    (folder/'SKILL.md').write_text('---\nname: reader\nallowed-tools: read_pdf\n---\nRead source pages.')
    store=ProjectStore(tmp_path);runner=TaskRunner(store,Provider())
    item=next(s for s in runner.skills.discover() if s.get('name')=='reader')
    runner.registry.set_enabled('pdf',False)
    with pytest.raises(HarnessError) as error:await runner.start('Read',skill_id=item['id'])
    assert error.value.code=='skill_dependency'
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    store.close()


def test_authored_skill_roundtrips_and_does_not_replace_existing(tmp_path):
    store=ProjectStore(tmp_path);loader=SkillLoader(store)
    first=loader.create_draft('source-summary','Summarise evidence: preserve citations.','Read each cited page.\nDraft a concise summary.',['read_pdf','stage_markdown'])
    second=loader.create_draft('source-summary','A separate revision','Different instructions.',['read_file'])
    assert first['id']!=second['id']
    assert loader.load(first['id'])['body']=='Read each cited page.\nDraft a concise summary.'
    assert loader.load(first['id'])['required_tools']==['read_pdf','stage_markdown']
    assert store.db.execute('SELECT count(*) FROM task_skills').fetchone()[0]==0
    assert first['scripts_execution']=='disabled'
    store.close()


def test_skill_authoring_rejects_path_escape_and_storage_symlink(tmp_path):
    project=tmp_path/'project';project.mkdir();outside=tmp_path/'outside';outside.mkdir()
    store=ProjectStore(project);loader=SkillLoader(store)
    with pytest.raises(HarnessError):loader.create_draft('../escape','description','instructions',[])
    (store.state/'skills').symlink_to(outside,target_is_directory=True)
    with pytest.raises(HarnessError) as error:loader.create_draft('safe-name','description','instructions',[])
    assert error.value.code=='skill_symlink'
    assert not list(outside.iterdir())
    store.close()


def test_pdf_health_reports_missing_ocr_dependencies(tmp_path,monkeypatch):
    import localoffice.plugins as plugins
    monkeypatch.setenv('HOME',str(tmp_path/'empty-home'))
    monkeypatch.setattr(plugins.shutil,'which',lambda name:None)
    store=ProjectStore(tmp_path)
    item=next(p for p in first_party_registry(store).catalogue() if p['id']=='pdf')
    assert item['health']=='degraded'
    assert any('tesseract' in issue for issue in item['issues'])
    assert any('por' in issue for issue in item['issues'])
    store.close()
