import os
import tempfile
from pathlib import Path
os.environ['INPUTSHAPER_PASSWORD'] = 'test-password'
os.environ['INPUTSHAPER_DATA'] = tempfile.mkdtemp()
from fastapi.testclient import TestClient
from app.main import app, db
from app.analysis import load_measurements
import pytest

def test_auth_csrf_and_persistence():
    with TestClient(app) as client:
        assert client.get('/api/runs').status_code == 401
        assert client.post('/api/login', json={'password':'wrong'}).status_code == 401
        assert client.post('/api/login', json={'password':'test-password'}).status_code == 200
        assert client.post('/api/runs', headers={'Origin':'https://evil.example'}).status_code == 403
        with db() as c:
            c.execute("INSERT INTO runs(id,title,filename,created,status,settings) VALUES('test','Title','test.stdata',1,'failed','{}')")
        note = {'text':'Changed belt tension', 'measurement':0, 'low':40, 'high':45}
        assert client.post('/api/runs/test/notes', json=note).status_code == 200
        assert client.post('/api/runs/test/notes', json={**note,'high':30}).status_code == 400
        view = {'axes':['Z'], 'filter':'EI', 'warning':False, 'xRange':[30,80]}
        assert client.put('/api/runs/test/view', json={'view':view}).status_code == 200
        saved = client.get('/api/runs/test').json()
        assert saved['notes'][0]['text'] == note['text']
        assert saved['view'] == view
        assert client.get('/api/runs/test/export').json()['view'] == view
        assert client.get('/api/runs/missing').status_code == 404
        assert client.post('/api/runs', files={'file':('bad.txt',b'bad')}).status_code == 400
        assert client.post('/api/logout').status_code == 200
        assert client.get('/api/runs').status_code == 401

def test_unsafe_legacy_rejected(tmp_path):
    f = tmp_path / 'old.stdata'
    f.write_bytes(b'\x80\x04pickle')
    with pytest.raises(ValueError, match='Legacy pickle'):
        load_measurements(f)

def test_nonmonotonic_rejected(tmp_path):
    import zstandard, json
    f = tmp_path / 'bad.stdata'
    f.write_bytes(zstandard.ZstdCompressor().compress(json.dumps({'samples':[[0,1,2,3]]*1024}).encode()))
    with pytest.raises(ValueError, match='strictly increasing'):
        load_measurements(f)
