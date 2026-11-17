"""Persistence observations: synthetic controls plus optional original archives."""
import json
from pathlib import Path

import numpy as np
import pytest

from app.analysis import analyze_measurement, load_measurements, shaper_calibrate
from app.result_summary import summarize_result
from app.spectral_features import THRESHOLDS, persistent_spectral_features
from shaketune.helpers.spectrogram import compute_spectrogram


def chirp_samples(with_line=False, duration=80):
    t = np.arange(0, duration, 1 / 1024)
    a = np.column_stack((t, np.random.default_rng(45).normal(0, 2, (len(t), 3))))
    # A 10–330 Hz linear sweep crossing the stationary candidate as well.
    a[:, 1] += 200 * np.sin(2 * np.pi * (10 * t + 2 * t * t))
    if with_line:
        a[:, 2] += 30 * np.sin(2 * np.pi * 278 * t)
    return a


def features(a, maximum=500):
    power, times, frequencies = compute_spectrogram(a)
    return persistent_spectral_features(power, times, frequencies, maximum)


def test_moving_linear_chirp_and_noise_are_not_persistent_candidates():
    result = features(chirp_samples())
    assert result['status'] == 'available'
    assert result['candidates'] == []


def test_stationary_line_survives_moving_chirp_and_noise():
    result = features(chirp_samples(with_line=True))
    assert len(result['candidates']) == 1
    candidate = result['candidates'][0]
    assert candidate['frequency_hz'] == pytest.approx(278, abs=result['frequency_resolution_hz'])
    assert candidate['median_mean_ratio'] > .6
    assert candidate['local_contrast_ratio'] > 1000
    assert candidate['temporal_coverage_fraction'] > .95
    assert 0 < candidate['peak_width_hz'] < 5
    assert features(chirp_samples(with_line=True), maximum=200)['candidates'] == []


def test_long_dwell_can_pass_and_is_explicitly_qualified():
    t = np.arange(0, 40, 1 / 1024)
    a = np.column_stack((t, np.random.default_rng(8).normal(0, 1, (len(t), 3))))
    a[t < 30, 1] += 30 * np.sin(2 * np.pi * 278 * t[t < 30])
    result = features(a)
    assert any(abs(c['frequency_hz'] - 278) < 2 for c in result['candidates'])
    assert 'long dwell or nonuniform sweep' in result['qualification']
    assert 'no excitation timeline is inferred' in result['qualification']
    assert 'not a stationarity proof' in result['qualification']


def test_unreduced_arrays_are_unchanged_and_candidate_count_is_bounded():
    frequency = np.arange(0., 501.)
    times = np.arange(20.)
    power = np.ones((len(frequency), len(times)))
    for i in range(30, 490, 35):
        power[i] = i * 10
    original = power.copy()
    result = persistent_spectral_features(power, times, frequency, 500)
    np.testing.assert_array_equal(power, original)
    assert len(result['candidates']) == THRESHOLDS['maximum_candidates'] == 8
    assert max(c['frequency_hz'] for c in result['candidates']) > 450
    assert all(c['median_mean_ratio'] == 1 for c in result['candidates'])
    assert all(c['temporal_coverage_fraction'] == 1 for c in result['candidates'])
    json.dumps(result, allow_nan=False)


def test_cutoff_bin_peak_is_checked_without_searching_beyond_requested_range():
    frequency = np.arange(0., 510.)
    times = np.arange(20.)
    power = np.ones((len(frequency), len(times)))
    power[499] = 100
    power[505] = 1000
    result = persistent_spectral_features(power, times, frequency, 499)
    assert [c['frequency_hz'] for c in result['candidates']] == [499]
    assert result['frequency_range_hz'] == [5, 499]
    assert result['candidates'][0]['peak_width_hz'] is None


def test_zero_and_short_records_are_distinguished():
    f = np.arange(100.)
    result = persistent_spectral_features(np.zeros((100, 20)), np.arange(20.), f, 80)
    assert result['status'] == 'available' and result['candidates'] == []
    short = persistent_spectral_features(np.ones((100, 3)), np.arange(3.), f, 80)
    assert short['status'] == 'unavailable' and short['candidates'] == []


def test_integration_uses_original_stft_and_retains_history_without_changing_psd(monkeypatch):
    a = chirp_samples(with_line=True)
    expected = features(a)
    # A deliberately uninformative display must not influence the descriptor.
    monkeypatch.setattr('app.diagnostics.reduce_spectrogram', lambda *args: {'power_db': [[0]]})
    result = analyze_measurement({'name': 'axis_X', 'samples': a}, {'test_type': 'static', 'max_freq': 500, 'scv': 5})
    assert result['spectral_features'] == expected
    assert result['spectrogram'] == {'power_db': [[0]]}
    calibration = shaper_calibrate.ShaperCalibrate(printer=None).process_accelerometer_data('axis_X', a)
    mask = calibration.freq_bins <= 500
    for axis, key in [('XYZ', 'psd_sum'), ('X', 'psd_x'), ('Y', 'psd_y'), ('Z', 'psd_z')]:
        np.testing.assert_array_equal(result['raw_psd'][axis], getattr(calibration, key)[mask])
    calibration.normalize_to_frequencies()
    np.testing.assert_array_equal(result['psd']['XYZ'], calibration.psd_sum[mask])
    summary = summarize_result({'measurements': [result]})['measurements'][0]
    assert summary['spectral_features'] == expected
    assert summary['signal_profile'] == result['signal_profile']
    assert 'spectrogram' not in summary


ARCHIVE = Path(__file__).resolve().parents[2] / 'printer-study' / 'recordings'


@pytest.mark.parametrize('axis', ['X', 'Y'])
def test_original_ebb_line_is_present_and_galvanic_has_no_comparable_candidate(axis):
    ebb = ARCHIVE / f'inputshaper_ebb36adxl_20261115_213512_axis_{axis}.stdata'
    galvanic = ARCHIVE / f'inputshaper_galvanic_20261115_222322_axis_{axis}.stdata'
    if not ebb.exists() or not galvanic.exists():
        pytest.skip('Original study recordings are local evidence, not repository fixtures.')
    ebb_result = features(load_measurements(ebb)[0]['samples'])
    galvanic_result = features(load_measurements(galvanic)[0]['samples'])
    candidates = [c for c in ebb_result['candidates'] if 276 <= c['frequency_hz'] <= 281]
    assert len(candidates) == 1
    assert .93 <= candidates[0]['median_mean_ratio'] <= .98
    assert candidates[0]['temporal_coverage_fraction'] > .9
    assert candidates[0]['local_contrast_ratio'] > 1000
    assert not galvanic_result['candidates']


@pytest.mark.parametrize('stamp', ['20261024_163012', '20261104_080630', '20261107_141214'])
def test_early_sustained_band_is_retained_without_a_component_label(stamp):
    path = ARCHIVE / f'inputshaper_{stamp}_axis_X.stdata'
    if not path.exists():
        pytest.skip('Original study recordings are local evidence, not repository fixtures.')
    result = features(load_measurements(path)[0]['samples'])
    candidates = [c for c in result['candidates'] if 180 <= c['frequency_hz'] <= 211]
    assert candidates
    assert max(c['median_mean_ratio'] for c in candidates) >= .75
    assert not any('component' in key or 'diagnosis' in key for c in candidates for key in c)
