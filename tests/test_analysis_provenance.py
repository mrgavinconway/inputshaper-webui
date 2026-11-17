import json

import numpy as np
import pytest
import zstandard

from app.analysis import ENGINE, analyze, analyze_upload, analyze_measurement, load_measurements, recording_timestamp
from app.diagnostics import band_power, quality
from app.result_summary import measurement_identity, summarize_result


def samples():
    t=np.arange(0,4,.001)
    return np.column_stack((t,100*np.sin(2*np.pi*42*t),20*np.sin(2*np.pi*70*t),np.full(len(t),9800)))


def write_recordings(path,names):
    values=samples().tolist()
    payload='\n'.join(json.dumps({'name':name,'samples':values}) for name in names)
    path.write_bytes(zstandard.ZstdCompressor().compress(payload.encode()))


@pytest.mark.parametrize('name',['vib_an45_00sp100_00_20261117_120000','belt_A_20261117_120000',
                                'belt_X_20261117_120000','axesmap_X_20261117_120000',
                                'staticfreq_X_20261117_120000'])
def test_non_sweep_recordings_do_not_get_shaper_recommendations(name):
    with pytest.raises(ValueError,match='not an input-shaper sweep'):
        analyze_measurement({'name':name,'samples':samples()},{'test_type':'shaper','scv':5,'max_freq':200})


def test_source_name_also_prevents_wrong_shaper_analysis(tmp_path):
    path=tmp_path/'axesmap_20261117_120000.stdata'
    write_recordings(path,['axis_X'])
    with pytest.raises(ValueError,match='not an input-shaper sweep'):
        analyze(path,{'scv':5,'max_freq':200})


def test_real_corexz_belt_names_are_supported(tmp_path):
    write_recordings(tmp_path/'source.stdata',['belt_X_20261117_120000','belt_Z_20261117_120005'])
    result=analyze_upload(tmp_path,{'test_type':'belts','kinematics':'corexz','scv':5,'max_freq':200})
    assert result['belt_analysis']['first_label']=='X'
    assert result['belt_analysis']['second_label']=='Z'
    assert result['belt_analysis']['similarity_percent']==pytest.approx(100)
    assert all(not m['filters'] for m in result['measurements'])
    with pytest.raises(ValueError,match='A and B'):
        analyze_upload(tmp_path,{'test_type':'belts','kinematics':'corexy','scv':5,'max_freq':200})


def test_source_and_canonical_sample_identity_survive_renaming(tmp_path):
    first=tmp_path/'inputshaper_20261117_120000_axis_X.stdata'
    second=tmp_path/'renamed.stdata'
    write_recordings(first,['axis_X_20261117_120001'])
    original=load_measurements(first)[0]
    # Different container bytes/name/compression, identical numeric samples.
    second.write_bytes(zstandard.ZstdCompressor(level=1).compress(json.dumps({'name':'renamed_X','samples':samples().tolist()}).encode()))
    renamed=load_measurements(second)[0]
    assert original['source_sha256']!=renamed['source_sha256']
    assert measurement_identity(original)==measurement_identity(renamed)
    assert original['source_measurement_index']==0
    assert original['recorded_at']=='2026-11-17T12:00:00'
    assert 'timezone unknown' in original['recorded_at_source']
    assert renamed['recorded_at'] is None


def test_manifest_original_filename_supplies_inferred_date(tmp_path):
    write_recordings(tmp_path/'source.stdata',['axis_X_20261117_120001'])
    (tmp_path/'sources.json').write_text(json.dumps([{'stored':'source.stdata','filename':'inputshaper_20261117_120000_axis_X.stdata'}]))
    result=analyze_upload(tmp_path,{'test_type':'static','scv':5,'max_freq':200})
    m=result['measurements'][0]
    assert m['recorded_at']=='2026-11-17T12:00:00'
    assert m['source_file']=='inputshaper_20261117_120000_axis_X.stdata'
    assert m['signal_profile']['dominant_dc_axis']=='+Z'
    assert m['signal_profile']['gravity_plausible']
    assert summarize_result(result)['measurements'][0]['measurement_sha256']==m['measurement_sha256']


def test_profile_cell_context_is_preserved_per_measurement(tmp_path):
    write_recordings(tmp_path/'source.stdata',['vib_an45_00sp100_00_20261117_120001','vib_an135_00sp150_50_20261117_120002'])
    result=analyze_upload(tmp_path,{'test_type':'vibrations','scv':5,'max_freq':200})
    assert [(m['profile_angle'],m['profile_speed']) for m in result['measurements']]==[(45,100),(135,150.5)]


def test_invalid_dates_are_not_invented_and_inner_timestamp_is_fallback():
    assert recording_timestamp('inputshaper_20269999_999999.stdata','axis_X')[0] is None
    assert recording_timestamp('untimed.stdata','axis_X_20261117_120000')==('2026-11-17T12:00:00','measurement name (inferred; timezone unknown)')


def test_raw_psd_units_and_sum_agree_with_known_tone_variance():
    m=analyze_measurement({'name':'axis_X','samples':samples()},{'test_type':'static','scv':5,'max_freq':200})
    high=m['frequency'][-1]
    assert band_power(m,0,high,'X')==pytest.approx(100**2/2,rel=.001)
    assert band_power(m,0,high,'XYZ')==pytest.approx((100**2+20**2)/2,rel=.001)
    assert band_power(m,0,high,'Z')==pytest.approx(0)
    np.testing.assert_allclose(m['raw_psd']['XYZ'],np.sum([m['raw_psd'][k] for k in 'XYZ'],axis=0))


def test_engine_damping_is_explicitly_qualified():
    m=analyze_measurement({'name':'axis_X','samples':samples()},{'test_type':'shaper','scv':5,'max_freq':200})
    assert ENGINE['adapter']==3
    assert m['damping_status']=='estimated'
    assert 'frequency-normalised PSD' in m['damping_method']
    assert 'not measured physical damping' in m['damping_method']


def test_sparse_gap_screening_boundaries():
    t=np.arange(10000)/3300
    data=np.column_stack([t,np.sin(t*100),np.cos(t*90),np.sin(t*120)])
    # Two consecutive missing points create exactly three nominal periods.
    sparse=np.delete(data,[5000,5001],axis=0)
    assert quality(sparse)['status']=='review'
    # One extra missing point crosses the maximum isolated-gap screen.
    substantial=np.delete(data,[5000,5001,5002],axis=0)
    assert quality(substantial)['status']=='invalid'
    exactly_at_fraction=np.delete(data,np.arange(500,9501,1000),axis=0)
    assert quality(exactly_at_fraction)['estimated_missing_fraction']==pytest.approx(.001)
    assert quality(exactly_at_fraction)['status']=='review'
    above_fraction=np.delete(data,np.arange(500,9501,900),axis=0)
    assert quality(above_fraction)['status']=='invalid'
