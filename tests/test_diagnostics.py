import json
import time
import uuid
from pathlib import Path
import numpy as np
import pytest
import zstandard
from fastapi.testclient import TestClient
from app.diagnostics import quality, peaks, match_peaks, compare, reduce_spectrogram, signal_profile, compatibility
from app.result_summary import measurement_digest, source_digest, measurement_identity, summarize_result
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


def timing_samples(count=10000):
    time_axis=np.arange(count)/3300
    return np.column_stack((time_axis, np.sin(time_axis*100), np.cos(time_axis*110), np.sin(time_axis*120)+9800))


def test_sparse_dropped_samples_retain_spectra_with_explicit_caveat():
    samples=np.delete(timing_samples(), [1500,4500,7500], axis=0)
    q=quality(samples)
    assert q['status']=='review'
    assert q['gap_count']==q['estimated_missing_samples']==3
    assert q['largest_gap_periods']==pytest.approx(2)
    assert q['estimated_missing_fraction']==pytest.approx(.0003)
    assert any('no samples were repaired' in warning for warning in q['warnings'])
    m=analyze_measurement_for_test(samples)
    assert m['raw_psd']['XYZ'] and m['frequency']


def analyze_measurement_for_test(samples):
    from app.analysis import analyze_measurement
    return analyze_measurement({'name':'axis_X','samples':samples},{'test_type':'static','max_freq':200,'scv':5})


@pytest.mark.parametrize('drop', [list(range(5000,5004)), list(range(100,9900,50))])
def test_material_sampling_loss_withholds_spectra(drop):
    samples=np.delete(timing_samples(),drop,axis=0)
    assert quality(samples)['status']=='invalid'
    assert analyze_measurement_for_test(samples)['frequency']==[]


def test_timing_jitter_without_long_gaps_is_invalid():
    samples=timing_samples()
    samples[:,0]=np.cumsum(np.resize([.7,1.3],len(samples)))/3300
    q=quality(samples)
    assert q['gap_count']==0 and q['status']=='invalid'


def test_sampling_and_static_acquisition_conditions_are_compared():
    a=spectrum();b=spectrum()
    a['quality'].update(sample_rate_hz=3300,resolution_hz=1.611)
    b['quality'].update(sample_rate_hz=3301,resolution_hz=1.612)
    assert compatibility(a,b,{},{})['comparable']
    b['quality']['sample_rate_hz']=1000
    assert any('Sample rate' in reason for reason in compatibility(a,b,{},{})['blockers'])
    b['quality']['sample_rate_hz']=3300;b['quality']['resolution_hz']=3.2
    assert any('Frequency-bin spacing' in reason for reason in compatibility(a,b,{},{})['blockers'])
    b['quality']['resolution_hz']=1.611
    assert not compatibility(a,b,{'test_type':'static','static_frequency':40},
                             {'test_type':'static','static_frequency':80})['comparable']
    a['profile_speed']=100;b['profile_speed']=150
    assert any('profile_speed' in reason for reason in compatibility(a,b,{},{})['blockers'])


def test_apparent_gravity_mapping_change_blocks_axis_comparison():
    samples=timing_samples()
    a=spectrum();b=spectrum()
    a['signal_profile']=signal_profile(samples)
    changed=samples.copy();changed[:,[2,3]]=changed[:,[3,2]]
    b['signal_profile']=signal_profile(changed)
    check=compatibility(a,b,{},{})
    assert a['signal_profile']['dominant_dc_axis']=='+Z'
    assert b['signal_profile']['dominant_dc_axis']=='+Y'
    assert not check['comparable']
    assert check['orientation_change_degrees']==pytest.approx(90,abs=.01)
    assert compatibility(a,a,{},{})['orientation_change_degrees']==pytest.approx(0)
    # A near-zero mean is not evidence of a known gravity direction.
    b['signal_profile']=signal_profile(samples*np.array([1,1,1,0]))
    assert not b['signal_profile']['gravity_plausible']
    assert compatibility(a,b,{},{})['comparable']


def test_recording_fingerprint_tracks_samples_not_names_or_container(tmp_path):
    samples=timing_samples(2000)
    digest=measurement_digest(samples)
    assert measurement_digest(samples.tolist())==digest
    assert measurement_digest(samples.astype('>f8'))==digest
    shifted=samples.copy();shifted[:,0]+=1
    assert measurement_digest(shifted)!=digest
    altered=samples.copy();altered[100,1]+=.01
    assert measurement_digest(altered)!=digest
    a={'measurement_sha256':digest,'source_file':'old.stdata'}
    b={'measurement_sha256':digest,'source_file':'renamed.stdata'}
    assert measurement_identity(a)==measurement_identity(b)
    assert measurement_identity({'source_sha256':digest,'source_measurement_index':0})!=measurement_identity({'source_sha256':digest,'source_measurement_index':1})
    assert measurement_identity({'name':'legacy'}) is None
    path=tmp_path/'source';path.write_bytes(b'recording')
    import hashlib
    assert source_digest(path)==hashlib.sha256(b'recording').hexdigest()


def test_summary_preserves_comparison_evidence_omits_heavy_plot_arrays():
    measurement=spectrum()
    measurement.update(waveform={'time':[1]},spectrogram={'power_db':[[1]]},
                       spectrograms={'XYZ':{'power_db':[[1]]}},
                       filters=[{'type':'MZV','frequency':42,'vals':[1,2]}],
                       signal_profile=signal_profile(timing_samples()),
                       measurement_sha256='a'*64,recorded_at='2026-11-17T12:00:00')
    source={'settings':{'test_type':'shaper'},'engine':{'adapter':2},'measurements':[measurement]}
    result=summarize_result(source);saved=result['measurements'][0]
    assert saved['frequency']==measurement['frequency'] and saved['raw_psd']==measurement['raw_psd']
    assert saved['signal_profile']==measurement['signal_profile']
    assert saved['measurement_sha256']=='a'*64 and saved['measurement']==0
    assert saved['filters']==[{'type':'MZV','frequency':42}]
    assert not {'waveform','spectrogram','spectrograms','psd'}.intersection(saved)
    assert compare(saved,saved,{},{},20,90)['axes']['XYZ']['change_percent']==pytest.approx(0)
