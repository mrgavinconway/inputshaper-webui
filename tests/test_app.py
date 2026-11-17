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

def test_xy_pair_upload_analysis_and_originals(tmp_path):
    import io
    import time
    import zipfile
    from app.selftest import fixture
    f = tmp_path / 'recording.stdata'
    fixture(f)
    raw = f.read_bytes()
    with TestClient(app) as client:
        client.post('/api/login', json={'password':'test-password'})
        response = client.post('/api/runs', files=[('file', ('inputshaper_X.stdata', raw)), ('file', ('inputshaper_Y.stdata', raw))])
        assert response.status_code == 202
        run_id = response.json()['id']
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            saved = client.get('/api/runs/'+run_id).json()
            if saved['status'] in ('ready', 'failed'):
                break
            time.sleep(.2)
        assert saved['status'] == 'ready', saved.get('error')
        measurements = saved['result']['measurements']
        assert [m['source_file'] for m in measurements] == ['inputshaper_X.stdata', 'inputshaper_Y.stdata']
        assert len(measurements) == 2
        assert all(len(m['filters']) == 5 for m in measurements)
        response = client.get('/api/runs/'+run_id+'/source')
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.read('1-inputshaper_X.stdata') == raw
            assert archive.read('2-inputshaper_Y.stdata') == raw
        assert client.post('/api/runs', files=[('file', ('x.stdata', raw)), ('file', ('y.txt', raw))]).status_code == 400
        assert client.post('/api/runs', files=[('file', ('x.stdata', raw))]*3).status_code == 400
