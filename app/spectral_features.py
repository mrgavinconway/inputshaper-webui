"""Descriptive persistence clues from the original, unreduced total STFT power.

This is an empirical screen, not a calibrated stationarity test or a source
classifier. The pinned Shake&Tune spectrogram uses detrended Kaiser-windowed
segments with 50% overlap; adjacent windows are not independent observations.
"""
import numpy as np


THRESHOLDS = {
    'minimum_frequency_hz': 5.0,
    'minimum_time_windows': 8,
    'minimum_median_mean_ratio': 0.6,
    'minimum_local_contrast_ratio': 4.0,
    'window_background_multiplier': 3.0,
    'minimum_temporal_coverage_fraction': 0.6,
    'background_flank_inner_hz': 10.0,
    'background_flank_outer_hz': 40.0,
    'minimum_background_bins': 6,
    'minimum_peak_separation_hz': 10.0,
    'maximum_candidates': 8,
}

QUALIFICATION = (
    'Heuristic observation clues, not a stationarity proof, component diagnosis, '
    'or recording-quality gate. A long dwell or nonuniform sweep can also produce '
    'persistent power; no excitation timeline is inferred. Coverage is the fraction '
    'of overlapping STFT windows above the stated local-background threshold, not '
    'an independent-sample probability or exact duration. Peak width describes the '
    'temporal-median spectrum and is not a damping estimate. No candidates does '
    'not establish absence of background vibration or interference.'
)


def persistent_spectral_features(power, times, frequencies, max_freq):
    """Find a few locally concentrated peaks present through much of a recording.

    Median/mean rejects bins dominated by a short sweep crossing. Local contrast
    rejects a persistent broad noise floor. Per-window contrast additionally
    screens how often a candidate stands above its surrounding frequencies.
    Thresholds are heuristic and selected against synthetic chirps and this
    archive's sustained 184–208 Hz bands and 278–279 Hz line; they are not
    validated sensitivity/specificity or a mechanical acceptance specification.
    Input arrays are never modified and no display-max-pooled values are used.
    """
    p = np.asarray(power, dtype=float)
    t = np.asarray(times, dtype=float)
    f = np.asarray(frequencies, dtype=float)
    if p.shape != (len(f), len(t)) or f.ndim != 1 or t.ndim != 1:
        raise ValueError('STFT power must have frequency rows and time-window columns.')
    if not np.isfinite(p).all() or not np.isfinite(f).all() or not np.isfinite(t).all() or np.any(p < 0):
        raise ValueError('STFT inputs must be finite and power nonnegative.')
    if len(f) > 1 and np.any(np.diff(f) <= 0):
        raise ValueError('STFT frequencies must be increasing.')

    keep = (f >= THRESHOLDS['minimum_frequency_hz']) & (f <= max_freq)
    result = {
        'version': 1,
        'status': 'available',
        'source': 'original total XYZ STFT power before display reduction',
        'method': 'Temporal-median local peaks, screened by median/mean, flanking-band contrast and per-window coverage.',
        'power_units': 'recorded acceleration units squared/Hz; sensor calibration is not established',
        'qualification': QUALIFICATION,
        'coverage_definition': 'Fraction of STFT windows with candidate-bin power at least 3 times that window’s median flanking-band power.',
        'thresholds': dict(THRESHOLDS),
        'requested_max_frequency_hz': float(max_freq),
        'frequency_range_hz': [float(f[keep][0]), float(f[keep][-1])] if keep.any() else None,
        'frequency_resolution_hz': float(np.median(np.diff(f))) if len(f) > 1 else None,
        'time_window_count': len(t),
        'time_centers_s': [float(t[0]), float(t[-1])] if len(t) else None,
        'candidates': [],
    }
    if len(t) < THRESHOLDS['minimum_time_windows'] or np.count_nonzero(keep) < 3:
        result.update(status='unavailable', reason='Insufficient time windows or frequency bins for the persistence screen.')
        return result

    # One adjacent bin on each side is retained for peak detection at the chosen
    # display cutoff. Background and all reported candidates stay inside range.
    indices = np.flatnonzero(keep)
    start = max(0, indices[0] - 1)
    stop = min(len(f), indices[-1] + 2)
    p = p[start:stop]
    f = f[start:stop]
    keep = keep[start:stop]
    median = np.median(p, axis=1)
    mean = np.mean(p, axis=1)
    if not np.any(median[keep] > 0):
        return result
    floor = max(float(np.max(median[keep])) * np.finfo(float).eps, np.finfo(float).tiny)
    local_maxima = np.flatnonzero((median[1:-1] > median[:-2]) & (median[1:-1] >= median[2:])) + 1
    accepted_intervals = []
    for i in sorted(local_maxima, key=lambda index: median[index], reverse=True):
        if not keep[i] or mean[i] <= 0 or median[i] / mean[i] < THRESHOLDS['minimum_median_mean_ratio']:
            continue
        if any(lo <= f[i] <= hi for lo, hi in accepted_intervals):
            continue
        distance = np.abs(f - f[i])
        flanks = keep & (distance >= THRESHOLDS['background_flank_inner_hz']) & (distance <= THRESHOLDS['background_flank_outer_hz'])
        if np.count_nonzero(flanks) < THRESHOLDS['minimum_background_bins']:
            continue
        background = float(np.median(median[flanks]))
        contrast = float(median[i] / max(background, floor))
        if contrast < THRESHOLDS['minimum_local_contrast_ratio']:
            continue
        window_background = np.maximum(np.median(p[flanks], axis=0), floor)
        present = p[i] >= THRESHOLDS['window_background_multiplier'] * window_background
        coverage = float(np.mean(present))
        if coverage < THRESHOLDS['minimum_temporal_coverage_fraction']:
            continue

        # Describe the local lobe and avoid reporting several nearby maxima in
        # the same half-height span as separate clues. This is not a mode fit.
        half = background + (median[i] - background) / 2
        left = right = i
        while left > 0 and keep[left - 1] and median[left] > half:
            left -= 1
        while right < len(f) - 1 and keep[right + 1] and median[right] > half:
            right += 1
        lo = float(np.interp(half, [median[left], median[left + 1]], [f[left], f[left + 1]])) if median[left] <= half else None
        hi = float(np.interp(half, [median[right], median[right - 1]], [f[right], f[right - 1]])) if median[right] <= half else None
        width = hi - lo if lo is not None and hi is not None else None
        if lo is not None and hi is not None and any(lo <= entry['frequency_hz'] <= hi for entry in result['candidates']):
            continue
        separation = THRESHOLDS['minimum_peak_separation_hz']
        accepted_intervals.append((min(f[i] - separation, lo if lo is not None else f[i]),
                                   max(f[i] + separation, hi if hi is not None else f[i])))
        result['candidates'].append({
            'frequency_hz': float(f[i]),
            'median_power': float(median[i]),
            'mean_power': float(mean[i]),
            'median_mean_ratio': float(median[i] / mean[i]),
            'local_background_median_power': background,
            'local_contrast_ratio': contrast,
            'temporal_coverage_fraction': coverage,
            'temporal_coverage_windows': int(np.count_nonzero(present)),
            'peak_width_hz': width,
        })
        if len(result['candidates']) >= THRESHOLDS['maximum_candidates']:
            break
    result['candidates'].sort(key=lambda candidate: candidate['frequency_hz'])
    return result
