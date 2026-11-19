"""Real host SIGKILL fixtures; no models or user-managed processes involved."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import psutil
import pytest
from localoffice.worker_guard import alive, guarded_command


def wait_dead(pid, created, seconds=7):
    deadline=time.monotonic()+seconds
    while alive(pid,created) and time.monotonic()<deadline:
        time.sleep(.05)
    assert not alive(pid,created)


@pytest.mark.parametrize('ignore_term', [False, True])
def test_host_sigkill_stops_only_owned_worker(tmp_path, ignore_term):
    ready=tmp_path/'ready.json'
    code=f'''import os,signal,time,json,psutil
from pathlib import Path
signal.signal(signal.SIGTERM, {'signal.SIG_IGN' if ignore_term else 'signal.SIG_DFL'})
Path({str(ready)!r}).write_text(json.dumps([os.getpid(), psutil.Process().create_time()]))
time.sleep(60)
'''
    host_code=f'''import subprocess,sys,time,json
from localoffice.worker_guard import guarded_command
p=subprocess.Popen(guarded_command(sys.executable,'-c',{code!r}),start_new_session=True)
print(p.pid,flush=True)
time.sleep(60)
'''
    unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    host=subprocess.Popen([sys.executable,'-c',host_code],stdout=subprocess.PIPE,text=True)
    pid=None
    try:
        assert select.select([host.stdout],[],[],5)[0]
        pid=int(host.stdout.readline())
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
        worker_pid,created=json.loads(ready.read_text())
        assert worker_pid==pid  # exec preserves the measured worker PID
        watchers=psutil.Process(pid).children()
        assert len(watchers)==1
        watcher=watchers[0];watcher_created=watcher.create_time()
        host.kill();host.wait(timeout=3)
        wait_dead(pid,created)
        wait_dead(watcher.pid,watcher_created)
        assert unrelated.poll() is None
    finally:
        if host.poll() is None:host.kill();host.wait()
        unrelated.terminate();unrelated.wait()
        if pid:
            try:os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError:pass


def test_normal_worker_exit_closes_protocol_and_watcher():
    process=subprocess.Popen(guarded_command(sys.executable,'-c',
        'import os,time; print(os.getpid(),flush=True); time.sleep(.3)'),
        start_new_session=True,stdout=subprocess.PIPE,text=True)
    assert select.select([process.stdout],[],[],5)[0]
    assert int(process.stdout.readline())==process.pid
    watcher=psutil.Process(process.pid).children()[0]
    created=watcher.create_time()
    process.wait(timeout=3)
    assert select.select([process.stdout],[],[],1)[0]
    assert process.stdout.read()==''
    wait_dead(watcher.pid,created)


def test_identity_mismatch_is_not_alive():
    assert not alive(os.getpid(),psutil.Process().create_time()-1)


def test_host_death_cleans_stubborn_worker_child(tmp_path):
    ready=tmp_path/'child.json'
    child_code=f'''import os,time,signal,json,psutil
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path({str(ready)!r}).write_text(json.dumps([os.getpid(),psutil.Process().create_time()]))
time.sleep(60)
'''
    worker_code=f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child_code!r}]); time.sleep(60)'
    host_code=f'''import subprocess,sys,time
from localoffice.worker_guard import guarded_command
p=subprocess.Popen(guarded_command(sys.executable,'-c',{worker_code!r}),start_new_session=True)
print(p.pid,flush=True)
time.sleep(60)
'''
    host=subprocess.Popen([sys.executable,'-c',host_code],stdout=subprocess.PIPE,text=True)
    pid=None
    try:
        assert select.select([host.stdout],[],[],5)[0]
        pid=int(host.stdout.readline())
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
        child_pid,created=json.loads(ready.read_text())
        host.kill();host.wait(timeout=3)
        wait_dead(child_pid,created)
    finally:
        if host.poll() is None:host.kill();host.wait()
        if pid:
            try:os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError:pass
