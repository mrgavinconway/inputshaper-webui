import numpy as np
import pytest

from app.changes import summarize_changes
from app.diagnostics import peaks, signal_profile


def recording(gain=1, direction='X', centers=(40,70), powers=(10,5), stop=100):
    frequency=np.linspace(0,stop,501)
    power=sum(gain*amplitude*np.exp(-((frequency-center)/2)**2) for center,amplitude in zip(centers,powers))
    return {'name':'test','excitation':direction,'frequency':frequency.tolist(),
            'raw_psd':{axis:power.tolist() for axis in ('XYZ','X','Y','Z')},
            'diagnostic_peaks':{axis:peaks(frequency,power) for axis in ('XYZ','X','Y','Z')},
            'quality':{'status':'pass','sample_rate_hz':3300,'resolution_hz':stop/500}}


def changes(current,baseline,sa=None,sb=None,low=20,high=90):
    return summarize_changes(current,baseline,sa or {},sb or {},{'adapter':3},{'adapter':3},low,high)


def test_gain_change_delta_is_signed_and_keeps_observations_separate_from_cause():
    baseline=recording();current=recording(gain=2)
    result=changes(current,baseline)
    assert result['compatibility']['comparable']
    assert result['axes']['XYZ']['change_percent']==pytest.approx(100)
    grid=result['delta']['frequency']
    np.testing.assert_allclose(result['delta']['psd']['XYZ'],np.interp(grid,baseline['frequency'],baseline['raw_psd']['XYZ']))
    assert any(o['kind']=='power' and 'rose' in o['title'] for o in result['observations'])
    assert 'not a diagnosis' in result['qualification']
    assert [p['key'] for p in result['annotation_prompts']]==['what_changed','expected_outcome','next_controlled_test']
    decreased=changes(recording(gain=.5),baseline)
    assert np.min(decreased['delta']['psd']['XYZ']) < 0


def test_delta_uses_union_native_grid_only_inside_selected_band():
    current=recording(stop=100.1);baseline=recording()
    result=changes(current,baseline,low=31.15,high=83.75)
    grid=np.asarray(result['delta']['frequency'])
    assert grid.min()==31.15 and grid.max()==83.75
    expected=np.unique(np.r_[31.15,83.75,
                            [f for f in current['frequency'] if 31.15<f<83.75],
                            [f for f in baseline['frequency'] if 31.15<f<83.75]])
    np.testing.assert_array_equal(grid,expected)
    assert 'no gain in spectral resolution' in result['delta']['method']
    with pytest.raises(ValueError,match='common measured'):
        changes(current,baseline,high=101)


@pytest.mark.parametrize('condition',['direction','sensor','sample_rate','orientation'])
def test_acquisition_changes_withhold_delta_but_retain_exploratory_overlay(condition):
    current=recording(gain=2);baseline=recording();sa={};sb={}
    if condition=='direction':current['excitation']='Y'
    if condition=='sensor':sa={'sensor':'EBB36'};sb={'sensor':'prior sensor'}
    if condition=='sample_rate':current['quality']['sample_rate_hz']=1000
    if condition=='orientation':
        data=np.column_stack((np.arange(2000)/1000,np.zeros(2000),np.zeros(2000),np.full(2000,9800)))
        baseline['signal_profile']=signal_profile(data)
        data[:,[2,3]]=data[:,[3,2]]
        current['signal_profile']=signal_profile(data)
    result=changes(current,baseline,sa,sb)
    assert not result['compatibility']['comparable']
    assert result['delta'] is None and result['axes']=={}
    assert result['overlay']['baseline']['raw_psd']==baseline['raw_psd']
    assert 'Exploratory overlay only' in result['overlay']['qualification']
    assert not any(o['kind'] in ('power','peaks','unmatched') for o in result['observations'])
    assert 'First resolve' in result['annotation_prompts'][2]['prompt']


def test_dominant_peak_swap_is_never_called_a_peak_frequency_shift():
    result=changes(recording(powers=(5,10)),recording(powers=(10,5)))
    assert not any(o['kind']=='peaks' for o in result['observations'])
    assert len(result['axes']['XYZ']['peaks']['matched'])==2
    shifted=changes(recording(centers=(43,70)),recording())
    assert any(o['kind']=='peaks' for o in shifted['observations'])


def test_sub_bin_peak_changes_are_not_highlighted_as_resolved_shifts():
    current=recording();baseline=recording()
    current['diagnostic_peaks']['XYZ'][0]['frequency']+=.1
    result=changes(current,baseline)
    assert not any(o['kind']=='peaks' for o in result['observations'])


def test_large_unmatched_feature_move_is_reported_without_equating_modes():
    result=changes(recording(centers=(49,70)),recording())
    assert not any(o['kind']=='peaks' for o in result['observations'])
    candidates=[o for o in result['observations'] if o['kind']=='unmatched']
    assert candidates and 'not automatically new/lost modes' in candidates[0]['detail']


def test_same_recording_fingerprint_is_not_presented_as_an_independent_trial():
    current=recording();baseline=recording()
    current['measurement_sha256']=baseline['measurement_sha256']='a'*64
    result=changes(current,baseline)
    assert any(o['title']=='Same original recording' for o in result['observations'])


def test_zero_baseline_power_has_no_invented_percentage():
    baseline=recording(gain=0)
    result=changes(recording(),baseline)
    assert result['axes']['XYZ']['change_percent'] is None
    assert any('percentage unavailable' in o['title'] for o in result['observations'])


def persistence(*frequencies, extent=(5, 500), status='available'):
    return {'status': status, 'frequency_range_hz': list(extent), 'frequency_resolution_hz': 1,
            'candidates': [{'frequency_hz': frequency, 'temporal_coverage_fraction': .95}
                           for frequency in frequencies]}


@pytest.mark.parametrize('side', ['baseline', 'current'])
@pytest.mark.parametrize('unavailable', [None,
    {'status': 'unavailable', 'reason': 'Insufficient time windows.', 'candidates': []},
    {'status': 'available', 'frequency_range_hz': None, 'candidates': []}])
def test_unavailable_persistence_screen_is_never_a_negative_detection(side, unavailable):
    baseline = recording(stop=500)
    current = recording(stop=500)
    baseline['spectral_features'] = persistence(278)
    current['spectral_features'] = persistence(278)
    target = baseline if side == 'baseline' else current
    target['spectral_features'] = unavailable
    observations = [o for o in changes(current, baseline)['observations'] if o['kind'] == 'persistence']
    assert len(observations) == 1
    assert observations[0]['title'] == 'Persistent frequency comparison unavailable'
    assert 'not a negative detection' in observations[0]['detail']
    assert 'detected only' not in observations[0]['detail'].lower()
    if unavailable and unavailable.get('reason'):
        assert unavailable['reason'] in observations[0]['detail']


@pytest.mark.parametrize('reverse', [False, True])
def test_persistent_candidate_outside_other_recording_range_is_not_an_appearance_or_loss(reverse):
    baseline = recording(stop=500)
    current = recording(stop=500)
    baseline['spectral_features'] = persistence(278, extent=(5, 500))
    current['spectral_features'] = persistence(extent=(5, 200))
    if reverse:
        baseline, current = current, baseline
    observations = [o for o in changes(current, baseline)['observations'] if o['kind'] == 'persistence']
    assert len(observations) == 1
    assert 'cannot be compared' in observations[0]['title']
    assert '278.0 Hz' in observations[0]['detail']
    assert 'appearance or disappearance cannot be inferred' in observations[0]['detail']
    assert 'detected only' not in observations[0]['detail'].lower()


def test_disjoint_persistence_ranges_only_report_comparison_limitation():
    baseline = recording(stop=500)
    current = recording(stop=500)
    baseline['spectral_features'] = persistence(50, extent=(5, 100))
    current['spectral_features'] = persistence(278, extent=(200, 500))
    observations = [o for o in changes(current, baseline)['observations'] if o['kind'] == 'persistence']
    assert len(observations) == 1
    assert 'no shared analysed range' in observations[0]['title']
    assert 'presence cannot be compared' in observations[0]['detail']


def test_persistent_candidate_change_in_shared_range_remains_descriptive():
    baseline = recording(stop=500)
    current = recording(stop=500)
    baseline['spectral_features'] = persistence(184)
    current['spectral_features'] = persistence(278)
    observations = [o for o in changes(current, baseline)['observations'] if o['kind'] == 'persistence']
    assert len(observations) == 1
    assert observations[0]['title'] == 'Persistent frequency candidates differ'
    detail = observations[0]['detail']
    assert 'shared 5–500 Hz' in detail
    assert 'baseline: 184.0 Hz' in detail and 'current: 278.0 Hz' in detail
    assert 'Screening thresholds and acquisition conditions affect detection' in detail
    assert 'does not establish absence' in detail
    assert 'no component is identified' in detail


def test_nearby_candidate_across_range_boundary_does_not_create_false_one_sided_detection():
    baseline = recording(stop=500)
    current = recording(stop=500)
    baseline['spectral_features'] = persistence(99, extent=(5, 100))
    current['spectral_features'] = persistence(101, extent=(5, 200))
    observations = [o for o in changes(current, baseline)['observations'] if o['kind'] == 'persistence']
    assert observations
    assert not any(o['title'] == 'Persistent frequency candidates differ' for o in observations)
    assert 'cannot be compared' in observations[0]['title']
