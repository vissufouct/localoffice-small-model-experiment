"""POSIX worker launcher with an independent parent-death watcher.

Exec preserves the worker PID (and RSS accounting). The watcher forks before
loading inference libraries, detaches, and closes all inherited protocol FDs.
Only a process group created by this launcher is eligible for cleanup.
"""
from __future__ import annotations
import os
import signal
import sys
import time
import psutil


def guarded_command(*command):
    host = psutil.Process()
    return [sys.executable, '-m', 'localoffice.worker_guard',
            str(host.pid), str(host.create_time()), *command]


def alive(pid, created):
    try:
        process = psutil.Process(pid)
        return process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def watch(host_pid, host_created, worker_pid, worker_created):
    while alive(worker_pid, worker_created):
        if not alive(host_pid, host_created):
            # Check ownership again immediately before signalling. Never discover
            # workers by names, ports or model paths belonging to other sessions.
            try:
                if alive(worker_pid, worker_created) and os.getpgid(worker_pid) == worker_pid:
                    members = [(worker_pid, worker_created)]
                    for child in psutil.Process(worker_pid).children(recursive=True):
                        try:
                            if os.getpgid(child.pid) == worker_pid:
                                members.append((child.pid, child.create_time()))
                        except (ProcessLookupError, psutil.NoSuchProcess):
                            pass
                    os.killpg(worker_pid, signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while any(alive(pid, born) for pid, born in members) and time.monotonic() < deadline:
                        time.sleep(.05)
                    for pid, born in members:
                        if alive(pid, born):
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
            return
        time.sleep(.2)


def main():
    host_pid, host_created = int(sys.argv[1]), float(sys.argv[2])
    command = sys.argv[3:]
    if not command or not alive(host_pid, host_created):
        return 1
    # Callers must provide a private session, preventing group-wide signalling
    # from ever reaching the host, desktop or another user's worker.
    worker_pid = os.getpid()
    if os.getpgrp() != worker_pid:
        return 2
    worker_created = psutil.Process().create_time()
    if os.fork() == 0:
        try:
            os.setsid()
            os.closerange(0, os.sysconf('SC_OPEN_MAX'))
            fd = os.open(os.devnull, os.O_RDWR)
            for target in (0, 1, 2):
                os.dup2(fd, target)
            watch(host_pid, host_created, worker_pid, worker_created)
        finally:
            os._exit(0)
    os.execvpe(command[0], command, os.environ)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
