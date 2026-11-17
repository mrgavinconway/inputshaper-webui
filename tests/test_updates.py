import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import Mock
import pytest

@pytest.fixture
def updater(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        monkeypatch.setitem(sys.modules, 'fcntl', types.SimpleNamespace(flock=lambda *a:None, LOCK_EX=1, LOCK_NB=2))
    path = Path(__file__).resolve().parents[1] / 'deploy/update.py'
    spec = importlib.util.spec_from_file_location('release_manager', path)
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    u.BASE = tmp_path / 'app'
    u.STATE = tmp_path / 'state'
    u.DATA = tmp_path / 'data'
    (u.BASE / 'current').mkdir(parents=True)
    u.DATA.mkdir()
    with sqlite3.connect(u.DATA / 'library.db') as c:
        c.execute('CREATE TABLE runs(status TEXT)')
        c.execute("INSERT INTO runs VALUES('ready')")
    u.current = Mock(return_value='oldsha')
    u.command = Mock(side_effect=lambda *a,**kw: 'newsha\trefs/heads/main' if a[:2]==('git','ls-remote') else '')
    u.switch = Mock()
    monkeypatch.setattr(sys,'argv',['update.py','install'])
    return u

def test_activation_failure_rolls_back(updater):
    u = updater
    u.healthy = Mock(side_effect=[False,True])
    with pytest.raises(RuntimeError, match='health check'):
        u.main()
    assert u.switch.call_count == 2
    assert u.switch.call_args.args[0] == (u.BASE/'current').resolve()
    assert u.healthy.call_args_list[1].args == ('oldsha',)
    with sqlite3.connect(u.DATA/'library.db') as c:
        assert c.execute('SELECT status FROM runs').fetchone()[0]=='ready'

def test_success_and_preflight_order(updater):
    u=updater
    u.healthy=Mock(return_value=True)
    u.main()
    assert json.loads((u.STATE/'status.json').read_text())['status']=='current'
    calls=[c.args for c in u.command.call_args_list]
    smoke=next(i for i,c in enumerate(calls) if c[-2:]==('-m','app.selftest'))
    stop=calls.index(('systemctl','stop','inputshaper.service'))
    assert smoke<stop

def test_busy_analysis_prevents_update(updater):
    u=updater
    with sqlite3.connect(u.DATA/'library.db') as c:
        c.execute("UPDATE runs SET status='processing'")
    u.main()
    assert not u.switch.called
    assert json.loads((u.STATE/'status.json').read_text())['available']
