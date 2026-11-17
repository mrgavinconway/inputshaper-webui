import json
import time
import uuid
from pathlib import Path
import numpy as np
import pytest
import zstandard
from fastapi.testclient import TestClient
from app.diagnostics import quality, peaks, match_peaks, compare, reduce_spectrogram
from app.analysis import analyze, analyze_upload
from app.selftest import fixture
from app.main import app, DATA, db

def spectrum(axis='X', powers=(10,5)):
    f=np.linspace(0,100,501)
    p=sum(power*np.exp(-((f-center)/2)**2) for center,power in zip([40,70],powers))
    return {'name':'test','excitation':axis,'frequency':f.tolist(),
            'raw_psd':{k:p.tolist() for k in ('XYZ','X','Y','Z')},
            'diagnostic_peaks':{k:peaks(f,p) for k in ('XYZ','X','Y','Z')},'quality':{'status':'pass'}}

def test_gap_is_flagged_and_spectral_analysis_withheld(tmp_path):
    p=tmp_path/'inputshaper_X.stdata';fixture(p)
    m=json.loads(zstandard.ZstdDecompressor().decompress(p.read_bytes()))
    for row in m['samples'][2000:]:row[0]+=.2
    p.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(m).encode()))
    result=analyze(p,{'scv':5,'max_freq':200,'test_type':'shaper'})['measurements'][0]
    assert result['quality']['status']=='invalid'
    assert result['quality']['gap_count']==1
    assert result['filters']==[] and result['frequency']==[]
    assert result['waveform']['time']

def test_dominant_peak_swap_does_not_become_frequency_shift():
    before=spectrum(powers=(10,5));after=spectrum(powers=(5,10))
    r=compare(after,before,{}, {},20,90)
    assert r['compatibility']['comparable']
    matched=r['axes']['XYZ']['peaks']['matched']
    assert len(matched)==2
    assert all(p['shift_hz']==0 for p in matched)
    assert matched[0]['power_change_percent']==pytest.approx(-50)
    assert matched[1]['power_change_percent']==pytest.approx(100)

def test_ambiguous_peaks_are_not_forced():
    result=match_peaks([{'frequency':40,'power':1}], [{'frequency':39,'power':1},{'frequency':41,'power':1}])
    assert not result['matched'] and len(result['unmatched_after'])==2

def test_cross_axis_and_acquisition_mismatch_block_numbers():
    assert not compare(spectrum('X'),spectrum('Y'),{},{},20,90)['axes']
    assert not compare(spectrum(),spectrum(),{'sensor_mount':'bed'},{'sensor_mount':'head'},20,90)['axes']
    assert not compare(spectrum(),spectrum(),{},{},20,90,{'adapter':2},{'adapter':1})['axes']
    assert not compare(spectrum('unknown'),spectrum(),{},{},20,90)['axes']

def test_band_power_and_no_extrapolation():
    a=spectrum();b=spectrum(powers=(20,10))
    r=compare(b,a,{},{},20,90)
    assert r['axes']['X']['change_percent']==pytest.approx(100)
    assert r['axes']['X']['after_rms']/r['axes']['X']['before_rms']==pytest.approx(np.sqrt(2))
    with pytest.raises(ValueError):compare(a,b,{},{},20,110)

def test_transient_survives_display_pooling():
    power=np.zeros((601,601));power[301,303]=100
    result=reduce_spectrogram(power,np.arange(601),np.arange(601),600)
    assert np.asarray(result['power_db']).max()==20

def make_recording(path,names):
    fixture(path)
    m=json.loads(zstandard.ZstdDecompressor().decompress(path.read_bytes()))
    path.write_bytes(zstandard.ZstdCompressor().compress(('\n'.join(json.dumps({**m,'name':name}) for name in names)).encode()))

@pytest.mark.parametrize('kind,names,extra',[('static',['staticfreq_X_20261117'],{}),
    ('belts',['belt_A_test','belt_B_test'],{'kinematics':'corexy'}),
    ('vibrations',['vib_an45_0sp40_0','vib_an135_0sp40_0','vib_an45_0sp60_0','vib_an135_0sp60_0'],{})])
def test_dedicated_analysis_types(tmp_path,kind,names,extra):
    make_recording(tmp_path/'source.stdata',names)
    result=analyze_upload(tmp_path,{'scv':5,'max_freq':200,'test_type':kind,**extra})
    assert not any(m['filters'] for m in result['measurements'])
    if kind=='static':assert set(result['measurements'][0]['spectrograms'])=={'XYZ','X','Y','Z'}
    if kind=='belts':assert result['belt_analysis']['similarity_percent']==pytest.approx(100)
    if kind=='vibrations':assert len(result['vibration_profile']['cells'])==4

def test_damping_fallback_label(tmp_path,monkeypatch):
    import shaketune.helpers.common_func as common
    import shaketune.graph_creators.computations.shaper_computation as engine
    def fallback(*args,**kwargs):return 42,None,10,False
    monkeypatch.setattr(common,'compute_mechanical_parameters',fallback)
    monkeypatch.setattr(engine,'compute_mechanical_parameters',fallback)
    p=tmp_path/'inputshaper_X.stdata';fixture(p)
    m=analyze(p,{'scv':5,'max_freq':200})['measurements'][0]
    assert m['damping']==.1 and m['damping_status']=='default (estimation failed)'

def test_repeatability_and_investigation_persistence():
    with TestClient(app) as client:
        client.post('/api/login',json={'password':'test-password'})
        refs=[]
        for amplitude in (10,11,20):
            run_id=uuid.uuid4().hex;folder=DATA/run_id;folder.mkdir()
            settings={'conditions':'unchanged'}
            result={'settings':settings,'engine':{'adapter':2},'measurements':[spectrum(powers=(amplitude,5))]}
            (folder/'result.json').write_text(json.dumps(result))
            with db() as c:c.execute('INSERT INTO runs(id,title,filename,created,status,settings) VALUES(?,?,?,?,?,?)',(run_id,'Test','test.stdata',time.time(),'ready',json.dumps(settings)))
            refs.append({'run_id':run_id,'measurement':0})
        payload={'current':refs[2],'baseline':refs[0],'repeats':[refs[0],refs[1],refs[2]],'low':20,'high':90}
        response=client.post('/api/compare',json=payload)
        assert response.status_code==200,response.text
        result=response.json();assert result['repeatability']['count']==2
        assert result['repeatability']['axes']['XYZ']['current_outside_observed_range']
        investigation_id=uuid.uuid4().hex
        body={'title':'Check fan contribution','hypothesis':'fan','expected':'feature follows speed','references':refs,'band':[35,45]}
        assert client.put('/api/investigations/'+investigation_id,json=body).status_code==200
        assert next(i for i in client.get('/api/investigations').json() if i['id']==investigation_id)['references']==refs
        saved=next(i for i in client.get('/api/investigations').json() if i['id']==investigation_id)
        assert saved['evidence'][0]['engine']=={'adapter':2}
        assert saved['evidence'][0]['band_power']['XYZ']>0

def test_reanalysis_preserves_notes_history_and_failed_attempt(tmp_path):
    p=tmp_path/'test.stdata';make_recording(p,['inputshaper_X_20261117'])
    with TestClient(app) as client:
        client.post('/api/login',json={'password':'test-password'})
        r=client.post('/api/runs',files={'file':('inputshaper_X.stdata',p.read_bytes())})
        run_id=r.json()['id']
        def wait():
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                r=client.get('/api/runs/'+run_id).json()
                if r['status'] not in ('queued','processing'):return r
                time.sleep(.1)
            pytest.fail('Analysis timed out')
        assert wait()['status']=='ready'
        client.post('/api/runs/'+run_id+'/notes',json={'text':'Preserve this annotation','measurement':0})
        assert client.put('/api/runs/'+run_id+'/context',json={'test_type':'static','axis_override':'X'}).status_code==200
        r=wait();assert r['status']=='ready' and r['notes'][0]['text']=='Preserve this annotation'
        assert list((DATA/run_id/'history').glob('*.json'))
        assert r['result']['test_type']=='static'
        # A failed type conversion must not discard the last successful result/settings.
        client.put('/api/runs/'+run_id+'/context',json={'test_type':'belts','kinematics':'corexy'})
        r=wait();assert r['status']=='ready' and r['error']
        assert r['result']['test_type']==r['settings']['test_type']=='static'
