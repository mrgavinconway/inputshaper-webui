import copy
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from app import main
from app.printer_config import build_config
from app.history import read_summary


def result():
    return {'test_type':'shaper','settings':{},'engine':{},'measurements':[
        {'name':f'axis_{axis}','excitation':axis,'choices':['MZV','EI'],
         'filters':[{'type':'EI','frequency':frequency+10},{'type':'MZV','frequency':frequency}],
         'damping':damping,'damping_status':'estimated','quality':{'status':'pass'}}
        for axis,frequency,damping in [('X',87.6,.095),('Y',59.8,.062)]]}


def test_recommended_config_uses_axis_values_and_recommendation_order():
    config=build_config(result())
    assert config['text']=='[input_shaper]\nshaper_freq_x: 87.6\nshaper_type_x: mzv\ndamping_ratio_x: 0.095\nshaper_freq_y: 59.8\nshaper_type_y: mzv\ndamping_ratio_y: 0.062\n'
    assert config['complete'] and not config['warnings']
    alternative=build_config(result(),{'X':'EI'},False)['text']
    assert 'shaper_freq_x: 97.6' in alternative and 'shaper_type_y: mzv' in alternative
    assert 'damping' not in alternative
    with pytest.raises(ValueError,match='recommended'):build_config(result(),{'X':'ZV'})


@pytest.mark.parametrize('status,damping',[('default (estimation failed)',.1),('unavailable',None),('estimated',float('nan')),('estimated',0),('estimated',1)])
def test_unavailable_damping_is_not_exported_as_an_estimate(status,damping):
    value=result();value['measurements'][0].update(damping_status=status,damping=damping)
    config=build_config(value)
    assert 'damping_ratio_x' not in config['text'] and 'damping_ratio_y' in config['text']
    assert any('X: damping' in message for message in config['warnings'])


def test_missing_ambiguous_invalid_axes_and_belts_never_invent_settings():
    value=result();value['measurements'].pop()
    config=build_config(value);assert not config['complete'] and 'shaper_freq_y' not in config['text']
    value['measurements'].append(copy.deepcopy(value['measurements'][0]))
    with pytest.raises(ValueError,match='unique'):build_config(value)
    value=result();value['measurements'][0]['quality']['status']='invalid'
    assert build_config(value)['axes']==['Y']
    value['test_type']='belts'
    with pytest.raises(ValueError,match='belt'):build_config(value)


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DATA',tmp_path)
    with TestClient(main.app) as connected:
        connected.post('/api/login',json={'password':'test-password'})
        yield connected


def seed(status='ready',filename='inputshaper_20261115_235959_axis_X.stdata'):
    run_id=uuid.uuid4().hex;folder=main.DATA/run_id;folder.mkdir()
    (folder/'result.json').write_text(json.dumps(result()))
    (folder/'source.stdata').write_bytes(b'original bytes')
    with main.db() as connection:
        connection.execute('INSERT INTO runs(id,title,filename,created,status,settings,view) VALUES(?,?,?,?,?,?,?)',
                           (run_id,'Source timestamp and friendly name',filename,123,status,'{}','{"axes":["Z"]}'))
    return run_id,folder


def test_config_endpoint_rebuilds_old_cache_and_preserves_saved_values(client):
    run_id,folder=seed()
    old={'summary_version':1,'settings':{},'engine':{},'measurements':[]}
    (folder/'summary.json').write_text(json.dumps(old))
    response=client.get(f'/api/runs/{run_id}/printer-config')
    assert response.status_code==200,response.text
    assert response.json()['text']==build_config(result())['text']
    assert read_summary(folder)['summary_version']==2
    assert client.get(f'/api/runs/{run_id}/printer-config?x=EI&include_damping=false').json()['text']==build_config(result(),{'X':'EI'},False)['text']
    assert client.get(f'/api/runs/{run_id}/printer-config?x=ZV').status_code==400


def test_bulk_delete_restore_preserves_files_notes_views_and_evidence(client):
    first,folder=seed();second,_=seed()
    client.post(f'/api/runs/{first}/notes',json={'text':'Keep this annotation','measurement':0})
    with main.db() as connection:connection.execute('INSERT INTO investigations(id,created,updated,body) VALUES(?,?,?,?)',('evidence',1,1,json.dumps({'references':[{'run_id':first,'measurement':0}]})))
    before=client.get(f'/api/runs/{first}').json();files={p.name:p.read_bytes() for p in folder.iterdir()}
    response=client.post('/api/imports/bulk',json={'run_ids':[first,second,first],'action':'delete'})
    assert response.json()['changed']==2 and client.get('/api/runs').json()==[]
    assert len(client.get('/api/runs?deleted=true').json())==2
    assert client.post('/api/imports/bulk',json={'run_ids':[first,second],'action':'restore'}).json()['changed']==2
    after=client.get(f'/api/runs/{first}').json()
    assert after==before
    assert {p.name:p.read_bytes() for p in folder.iterdir()}==files
    with main.db() as connection:assert first in connection.execute('SELECT body FROM investigations').fetchone()['body']


@pytest.mark.parametrize('problem,expected',[('missing',404),('queued',409),('processing',409),('deleted',409)])
def test_bulk_validation_is_atomic(client,problem,expected):
    first,_=seed();second,_=seed(problem if problem in ('queued','processing') else 'ready')
    if problem=='missing':second=uuid.uuid4().hex
    if problem=='deleted':client.delete(f'/api/runs/{second}')
    response=client.post('/api/imports/bulk',json={'run_ids':[first,second],'action':'delete'})
    assert response.status_code==expected
    assert main.row_run(first)['deleted'] is None


def test_bulk_auth_csrf_and_source_date(client):
    known,_=seed();unknown,_=seed(filename='untimed.stdata')
    rows={row['id']:row for row in client.get('/api/runs').json()}
    assert rows[known]['recorded_at']=='2026-11-15T23:59:59' and rows[unknown]['recorded_at'] is None
    body={'run_ids':[known],'action':'delete'}
    assert client.post('/api/imports/bulk',json=body,headers={'Origin':'https://evil.example'}).status_code==403
    client.post('/api/logout')
    assert client.post('/api/imports/bulk',json=body).status_code==401
