#!/usr/bin/env python3
"""Privileged release manager. Fixed upstream, atomic switch, health check and rollback."""
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

os.umask(0o022)

BASE = Path('/opt/inputshaper')
STATE = Path('/var/lib/inputshaper-update')
REPO = 'https://github.com/mrgavinconway/inputshaper-webui.git'
STATE.mkdir(parents=True, exist_ok=True)

def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, stderr=subprocess.STDOUT, text=True, timeout=600).strip()

def status(**data):
    value = {'checked': time.time(), **data}
    p = STATE / 'status.tmp'
    p.write_text(json.dumps(value))
    p.chmod(0o644)
    p.replace(STATE / 'status.json')

def current():
    path = BASE / 'current'
    return command('git', 'rev-parse', 'HEAD', cwd=path) if path.exists() else None

def switch(path):
    tmp = BASE / 'next'
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(path)
    tmp.replace(BASE / 'current')

def healthy(sha):
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2) as r:
                health = json.load(r)
                if health['status'] == 'ok' and health['version'] == sha:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

def main():
    with (STATE / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        old = current()
        status(status='checking', available=False, message='Checking main on GitHub…')
        latest = command('git', 'ls-remote', REPO, 'refs/heads/main').split()[0]
        if sys.argv[1] == 'check' or latest == old:
            status(status='available' if latest != old else 'current', latest=latest, available=latest != old,
                   message='A new version is ready to install.' if latest != old else 'You are up to date.')
            return
        # Recheck durable queue immediately before installing.
        dbpath = Path('/var/lib/inputshaper/library.db')
        if dbpath.exists():
            with sqlite3.connect(dbpath) as c:
                if c.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','processing')").fetchone()[0]:
                    status(status='available', latest=latest, available=True, message='Analyses are still running. Try again after they finish.')
                    return
        status(status='installing', latest=latest, available=False, message='Preparing isolated release and dependencies…')
        release = BASE / 'releases' / latest
        if not release.exists():
            command('git', 'clone', REPO, str(release))
        command('git', 'fetch', 'origin', 'main', cwd=release)
        command('git', 'checkout', '--detach', latest, cwd=release)
        if old:
            command('git', 'merge-base', '--is-ancestor', old, latest, cwd=release)
        command('python3', '-m', 'venv', str(release / '.venv'))
        python = str(release / '.venv/bin/python')
        command(python, '-m', 'pip', 'install', '-r', str(release / 'requirements.txt'))
        command(python, '-c', 'import app.analysis; import app.main', cwd=release)
        command(python, '-m', 'app.selftest', cwd=release)
        previous = (BASE / 'current').resolve() if (BASE / 'current').exists() else None
        command('systemctl', 'stop', 'inputshaper.service')
        backup = STATE / 'pre-update.db'
        if dbpath.exists():
            with sqlite3.connect(dbpath) as src, sqlite3.connect(backup) as dst:
                src.backup(dst)
        try:
            switch(release)
            command('systemctl', 'start', 'inputshaper.service')
            if not healthy(latest):
                raise RuntimeError('New service failed its health check')
        except Exception:
            command('systemctl', 'stop', 'inputshaper.service')
            if previous:
                switch(previous)
                if backup.exists():
                    with sqlite3.connect(backup) as src, sqlite3.connect(dbpath) as dst:
                        src.backup(dst)
                command('systemctl', 'start', 'inputshaper.service')
                if not healthy(old):
                    raise RuntimeError('Rollback started but failed health check; inspect service logs')
            raise
        status(status='current', latest=latest, available=False, message='Update installed; service restarted and health check passed.')

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        status(status='failed', available=False, message='Update did not complete. Existing release retained or restored. '+str(e)[-500:])
        raise
