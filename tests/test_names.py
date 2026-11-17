import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    with TestClient(main.app) as connected:
        connected.post('/api/login', json={'password': 'test-password'})
        yield connected


def seed(title='17 Nov 2026, 08:58:52 · X/Y pair'):
    run_id = uuid.uuid4().hex
    folder = main.DATA / run_id
    folder.mkdir()
    filenames = [f'inputshaper_20261117_085852_axis_{axis}.stdata' for axis in ('X', 'Y')]
    (folder / 'sources.json').write_text(json.dumps([{'filename': name, 'stored': f'{index}.stdata'} for index, name in enumerate(filenames)]))
    (folder / '0.stdata').write_bytes(b'original X recording')
    (folder / '1.stdata').write_bytes(b'original Y recording')
    result = {'settings': {'max_freq': 500}, 'engine': {'adapter': 3}, 'measurements': [
        {'name': 'axis_X', 'excitation': 'X', 'source_file': filenames[0], 'frequency': [0, 10],
         'raw_psd': {axis: [0, 1] for axis in ('XYZ', 'X', 'Y', 'Z')}}]}
    (folder / 'result.json').write_text(json.dumps(result))
    with main.db() as connection:
        connection.execute('INSERT INTO runs(id,title,filename,created,status,settings,view) VALUES(?,?,?,?,?,?,?)',
                           (run_id, title, ' + '.join(filenames), 123, 'ready', json.dumps(result['settings']), '{"axes":["Z"]}'))
    return run_id, folder


def test_rename_keeps_source_date_data_notes_and_reference_identity(client, monkeypatch):
    run_id, folder = seed()
    client.post(f'/api/runs/{run_id}/notes', json={'text': 'Keep this evidence', 'measurement': 0})
    before = client.get(f'/api/runs/{run_id}').json()
    original_bytes = {p.name: p.read_bytes() for p in folder.iterdir()}
    monkeypatch.setattr(main.executor, 'submit', lambda *args: pytest.fail('Renaming must not re-analyse'))
    response = client.put(f'/api/runs/{run_id}/name', json={'friendly_name': '  Umbilical supported   · fans off  '})
    assert response.status_code == 200
    expected = '17 Nov 2026, 08:58:52 · X/Y pair · Umbilical supported · fans off'
    assert response.json()['title'] == expected
    after = client.get(f'/api/runs/{run_id}').json()
    for field in ('id', 'created', 'filename', 'settings', 'view', 'notes', 'result', 'status'):
        assert after[field] == before[field]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == original_bytes
    assert client.get('/api/runs').json()[0]['title'] == expected
    assert client.get('/api/catalog').json()[0]['title'] == expected
    history = client.get('/api/history').json()['records'][0]
    assert history['friendly_name'] == 'Umbilical supported · fans off'
    assert history['recorded_at'] == '2026-11-17T08:58:52'
    assert history['run_id'] == run_id
    # A second rename replaces the friendly suffix; clearing restores the base.
    assert client.put(f'/api/runs/{run_id}/name', json={'friendly_name': 'Restored A'}).json()['title'].endswith('pair · Restored A')
    assert client.put(f'/api/runs/{run_id}/name', json={'friendly_name': '   '}).json()['title'] == before['title']


def test_legacy_custom_title_recovers_file_timestamp_when_renamed(client):
    run_id, _ = seed('Original custom label')
    detail = client.get(f'/api/runs/{run_id}').json()
    assert detail['base_title'] == '17 Nov 2026, 08:58:52 · X/Y pair'
    assert detail['friendly_name'] == 'Original custom label'
    renamed = client.put(f'/api/runs/{run_id}/name', json={'friendly_name': 'Revised label'}).json()
    assert renamed['title'] == '17 Nov 2026, 08:58:52 · X/Y pair · Revised label'


def test_name_migration_and_restart_preserve_old_library(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    with sqlite3.connect(tmp_path / 'library.db') as connection:
        connection.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,title TEXT,filename TEXT,created REAL,status TEXT,error TEXT,settings TEXT,view TEXT DEFAULT \'{}\')')
        connection.execute("INSERT INTO runs(id,title,filename,created,status,settings) VALUES('legacy','untimed_X','untimed_X.stdata',1,'failed','{}')")
    for name in ('First name', 'Second name'):
        with TestClient(main.app) as connected:
            connected.post('/api/login', json={'password': 'test-password'})
            response = connected.put('/api/runs/legacy/name', json={'friendly_name': name})
            assert response.json()['title'] == 'untimed_X · ' + name
            assert connected.get('/api/runs/legacy').json()['created'] == 1
    # Unknown recording dates remain filenames; an import date is never invented.
    assert main.row_run('legacy')['base_title'] == 'untimed_X'


def test_new_upload_treats_title_as_friendly_name(client, monkeypatch):
    monkeypatch.setattr(main.executor, 'submit', lambda *args: None)
    response = client.post('/api/runs', data={'title': 'Baseline A'}, files={'file': ('inputshaper_20261117_120000_axis_X.stdata', b'\x28\xb5\x2f\xfdtest')})
    assert response.status_code == 202
    detail = client.get('/api/runs/' + response.json()['id']).json()
    assert detail['title'] == '17 Nov 2026, 12:00:00 · Baseline A'
    assert detail['base_title'] == '17 Nov 2026, 12:00:00'
    assert detail['friendly_name'] == 'Baseline A'


def test_rename_validation_and_access(client):
    run_id, _ = seed()
    before = main.row_run(run_id)['title']
    assert client.put(f'/api/runs/{run_id}/name', json={'friendly_name': 'x' * 161}).status_code == 422
    assert client.put(f'/api/runs/{run_id}/name', json={'friendly_name': None}).status_code == 422
    assert main.row_run(run_id)['title'] == before
    assert client.put('/api/runs/missing/name', json={'friendly_name': 'Name'}).status_code == 404
    assert client.put(f'/api/runs/{run_id}/name', headers={'Origin': 'https://example.invalid'}, json={'friendly_name': 'Name'}).status_code == 403
    client.post('/api/logout')
    assert client.put(f'/api/runs/{run_id}/name', json={'friendly_name': 'Name'}).status_code == 401
