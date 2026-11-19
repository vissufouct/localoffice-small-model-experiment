import json
import shutil
import pytest
from localoffice.storage import ProjectStore
from localoffice.runtime import TaskRunner
from localoffice.errors import HarnessError
from test_word_worker import fixture_docx


class QuestionProvider:
    context_limit=4096
    def status(self):return {'loaded':True}
    async def count_tokens(self,messages):return 500
    async def stream(self,messages,**kwargs):
        yield {'type':'token','text':json.dumps({'tool':'ask_user','text':'Which audience?'})}
        yield {'type':'finish','reason':'stop'}


@pytest.mark.asyncio
@pytest.mark.parametrize('target,expected',[('new.docx','word-create'),('existing.DOCX','word-edit'),('note.md',None)])
async def test_default_word_skill_is_selected_and_only_one_body_loaded(tmp_path,target,expected):
    fixture_docx(tmp_path/'existing.DOCX')
    store=ProjectStore(tmp_path);runner=TaskRunner(store,QuestionProvider())
    # A project import with the same name must never replace the bundled default.
    fake=tmp_path/'.agents/skills/word-create';fake.mkdir(parents=True)
    (fake/'SKILL.md').write_text('---\nname: word-create\ndescription: Project skill\n---\nSHADOW IMPORT MUST NOT AUTOLOAD')
    try:
        task=await runner.start('Draft or edit',artifact=target);await runner.job
        row=store.db.execute('SELECT * FROM task_skills WHERE task_id=?',(task,)).fetchone()
        assert (row['name'] if row else None)==expected
        system=(await runner.assemble(task))[0]['content']
        assert 'SHADOW IMPORT MUST NOT AUTOLOAD' not in system
        for name in ['word-create','word-edit']:
            body=runner.skills.inspect(runner.skills.builtin_root/name/'SKILL.md')['body']
            assert (body in system)==(name==expected)
        catalog=runner.skills.discover()
        assert len([x for x in catalog if x.get('origin')=='bundled'])==2
        assert all('body' not in x for x in catalog)
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_default_skill_is_pinned_on_resume_and_explicit_choice_wins(tmp_path):
    store=ProjectStore(tmp_path);runner=TaskRunner(store,QuestionProvider())
    copied=tmp_path/'bundled-copy';shutil.copytree(runner.skills.builtin_root,copied)
    runner.skills.builtin_root=copied
    try:
        task=await runner.start('Create report',artifact='new.docx');await runner.job
        pinned=dict(store.db.execute('SELECT * FROM task_skills WHERE task_id=?',(task,)).fetchone())
        (copied/'word-create/SKILL.md').write_text('---\nname: word-create\ndescription: Changed version\n---\nCHANGED BUNDLED BODY')
        await runner.start('Continue',artifact='new.docx',task_id=task);await runner.job
        assert dict(store.db.execute('SELECT * FROM task_skills WHERE task_id=?',(task,)).fetchone())==pinned
        assert 'CHANGED BUNDLED BODY' not in (await runner.assemble(task))[0]['content']
        custom=runner.skills.create_draft('custom-word','Custom workflow','EXPLICIT WORKFLOW',['word.create'])
        other=await runner.start('Create another',artifact='other.docx',skill_id=custom['id']);await runner.job
        assert store.db.execute('SELECT name FROM task_skills WHERE task_id=?',(other,)).fetchone()[0]=='custom-word'
        store.close();store=ProjectStore(tmp_path);runner=TaskRunner(store,QuestionProvider())
        assert pinned['body'] in (await runner.assemble(task))[0]['content']
    finally:await runner.pause();store.close()


@pytest.mark.asyncio
async def test_default_skill_obeys_plugin_admission(tmp_path):
    store=ProjectStore(tmp_path);runner=TaskRunner(store,QuestionProvider())
    try:
        runner.registry.set_enabled('word',False)
        with pytest.raises(HarnessError,match='Enable'):
            await runner.start('Create',artifact='new.docx')
        assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    finally:store.close()
