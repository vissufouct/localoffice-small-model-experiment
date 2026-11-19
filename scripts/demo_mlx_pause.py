"""Real API pause/Continue: cancelled MLX worker reloads its selected local weights."""
import json,os,tempfile,time
from pathlib import Path
from fastapi.testclient import TestClient
from localoffice.server import Host,create_app

root=Path(tempfile.mkdtemp(prefix='localoffice-mlx-pause-'))
(root/'source.md').write_text('Project: Cedar. Budget: EUR 120. Owner: Ana.\n')
model=json.loads(Path('docs/benchmarks/approved-mlx-download.json').read_text())['local_path']
# Keep test project registration/preferences out of the user's desktop settings.
os.environ['HOME']=str(root/'test-home')
report={'status':'started','project':str(root),'model':model};began=time.monotonic()
try:
    with TestClient(create_app('fixture',Host())) as client:
        client.headers['Authorization']='Bearer fixture'
        def post(path,body):
            r=client.post(path,json=body)
            if r.status_code!=200:raise RuntimeError(r.text)
            return r.json()
        post('/project/open',{'path':str(root)})
        loaded=post('/model/load',{'path':model,'backend':'mlx','context':4096});old_pid=loaded['pid']
        task=post('/task',{'text':'Read source.md. Create note.md with the project, budget and owner. Stage it and finish.','mode':'live','artifact':'note.md'})['task_id']
        deadline=time.monotonic()+60
        while True:
            activity=client.get('/activity').json()
            if activity.get('partial'):break
            if not activity['running']:raise RuntimeError('Task ended before generation pause')
            if time.monotonic()>deadline:raise TimeoutError('No generation before deadline')
            time.sleep(.02)
        post('/pause',{})
        state=client.get('/model').json()
        assert not state['loaded'] and state['resumable'],state
        resumed=post('/task',{'text':'Continue the same task. Read source.md, create note.md with Cedar, EUR 120 and Ana, stage it and finish.','mode':'background','artifact':'note.md','task_id':task})
        assert resumed['task_id']==task
        assert client.get('/model').json()['pid']!=old_pid
        deadline=time.monotonic()+120
        while client.get('/activity').json()['running']:
            if time.monotonic()>deadline:raise TimeoutError('Continuation deadline')
            time.sleep(.1)
        snapshot=client.get('/project').json()
        assert next(t for t in snapshot['tasks'] if t['id']==task)['status']=='completed'
        candidate=next(c for c in snapshot['candidates'] if c['path']=='note.md')
        assert not (root/'note.md').exists()
        post('/apply',{'operation_id':candidate['operation_id'],'candidate_hash':candidate['candidate_hash'],'action':'save'})
        text=(root/'note.md').read_text()
        assert all(word in text for word in ['Cedar','120','Ana'])
        report.update(status='passed',same_task=True,worker_reloaded=True,original_absent_before_save=True,facts_preserved=True)
except Exception as exc:report.update(status='failed',error=str(exc))
report['elapsed_seconds']=time.monotonic()-began
Path('docs/benchmarks/mlx-pause-api.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
raise SystemExit(0 if report['status']=='passed' else 1)
