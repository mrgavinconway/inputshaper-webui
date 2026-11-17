"""Describe observable recording changes without inferring a mechanical cause."""
import numpy as np

from .diagnostics import CONTEXT, compare
from .result_summary import measurement_identity


AXES = ('XYZ', 'X', 'Y', 'Z')
UNKNOWN = (None, '', 'unknown', 'auto')


def _spacing(measurement):
    frequencies = np.asarray(measurement.get('frequency', []), dtype=float)
    return float(np.median(np.diff(frequencies))) if len(frequencies) > 1 else 0.0


def _persistence_observations(a, b):
    before = b.get('spectral_features') or {}
    after = a.get('spectral_features') or {}
    if not before and not after:
        return []
    unavailable = []
    ranges = []
    for label, features in (('Baseline', before), ('Current', after)):
        if features.get('status') != 'available':
            unavailable.append(label + ': ' + features.get('reason', 'persistence screen unavailable; re-analyse to enable it.'))
            continue
        try:
            extent = np.asarray(features.get('frequency_range_hz'), dtype=float)
            valid = extent.shape == (2,) and np.isfinite(extent).all() and 0 <= extent[0] < extent[1]
        except (TypeError, ValueError):
            valid = False
        if not valid:
            unavailable.append(label + ': the analysed persistence frequency range is unrecorded or invalid.')
        else:
            ranges.append(extent)
    if unavailable:
        return [{'kind': 'persistence', 'title': 'Persistent frequency comparison unavailable',
                 'detail': ' '.join(unavailable) + ' An unavailable screen is not a negative detection. No appearance or disappearance can be inferred from it.'}]
    common_low = max(extent[0] for extent in ranges)
    common_high = min(extent[1] for extent in ranges)
    if common_high <= common_low:
        return [{'kind': 'persistence', 'title': 'Persistence screens have no shared analysed range',
                 'detail': 'The two persistence screens cover different frequency ranges. Candidate presence cannot be compared; this does not establish appearance or disappearance of a physical signal.'}]

    before_lines = before.get('candidates', [])
    after_lines = after.get('candidates', [])
    shared = lambda line: common_low <= line['frequency_hz'] <= common_high
    before_shared = [line for line in before_lines if shared(line)]
    after_shared = [line for line in after_lines if shared(line)]
    resolutions = [features.get('frequency_resolution_hz') for features in (before, after)]
    spacings = [float(value) for value in resolutions if isinstance(value, (int, float)) and np.isfinite(value) and value > 0]
    tolerance = max(3.0, 2 * max([_spacing(a), _spacing(b), *spacings]))
    # A neighbour just outside the common range may still represent the same
    # unresolved feature. It must not create an artificial one-sided detection.
    before_only = [line for line in before_shared if not any(abs(line['frequency_hz'] - other['frequency_hz']) <= tolerance for other in after_lines)]
    after_only = [line for line in after_shared if not any(abs(line['frequency_hz'] - other['frequency_hz']) <= tolerance for other in before_lines)]

    def describe(lines):
        return ', '.join(f"{line['frequency_hz']:.1f} Hz ({100 * line['temporal_coverage_fraction']:.0f}% of windows above local background)" for line in lines) or 'none'

    observations = []
    outside_before = [line for line in before_lines if not shared(line)]
    outside_after = [line for line in after_lines if not shared(line)]
    if outside_before or outside_after:
        observations.append({'kind': 'persistence', 'title': 'Some persistent candidates cannot be compared across the analysed ranges',
                             'detail': 'Outside the shared analysed range: baseline ' + describe(outside_before) + '; current ' + describe(outside_after) + '. These candidates were not assessed at those frequencies in both recordings. Their appearance or disappearance cannot be inferred.'})
    if before_only or after_only:
        observations.append({'kind': 'persistence', 'title': 'Persistent frequency candidates differ',
                             'detail': f'Within the shared {common_low:g}–{common_high:g} Hz analysed persistence range: detected only in baseline: ' + describe(before_only) + '. Detected only in current: ' + describe(after_only) + '. Screening thresholds and acquisition conditions affect detection; a missing candidate does not establish absence of vibration or interference. Check stationary/fan/sensor conditions; no component is identified by these lines.'})
    return observations


def summarize_changes(a, b, sa, sb, ea, eb, low, high):
    """Current ``a`` versus baseline ``b``; all signed changes are current - baseline."""
    result = compare(a, b, sa, sb, low, high, ea, eb)
    check = result['compatibility']
    observations = []
    setup_changes = []
    context_keys = (*CONTEXT, 'static_frequency', 'sweep_start_hz', 'sweep_end_hz',
                    'sweep_duration_s', 'sweep_profile', 'acceleration_units', 'conditions')
    for key in context_keys:
        before, after = sb.get(key), sa.get(key)
        if before not in UNKNOWN and after not in UNKNOWN and before != after:
            setup_changes.append(key)
            observations.append({'kind': 'setup', 'title': key.replace('_', ' ').capitalize() + ' changed',
                                 'detail': f'Recorded baseline: {before}. Recorded current: {after}. This is context supplied for the test, not a cause established by the spectrum.'})

    if a.get('excitation', 'unknown') != b.get('excitation', 'unknown'):
        observations.append({'kind': 'setup', 'title': 'Different excitation directions',
                             'detail': f"Baseline {b.get('excitation', 'unknown')} → current {a.get('excitation', 'unknown')}. Overlay is exploratory; a numerical difference would mix different tests."})
    orientation = check.get('orientation_change_degrees')
    if orientation is not None and orientation > 5:
        observations.append({'kind': 'orientation', 'title': 'Apparent gravity direction changed',
                             'detail': f'The mean acceleration direction differs by {orientation:.1f}°. Check sensor mounting and axes mapping. This is an orientation clue, not a measured mechanical fault.'})
    for key, label, unit in (('sample_rate_hz', 'Sampling rate', 'samples/s'),
                             ('resolution_hz', 'Frequency-bin spacing', 'Hz')):
        before = b.get('quality', {}).get(key)
        after = a.get('quality', {}).get(key)
        if before and after and abs(after-before)/max(before, after) > .001:
            observations.append({'kind': 'sampling', 'title': label + ' changed',
                                 'detail': f'{before:.5g} → {after:.5g} {unit}. Small apparent peak shifts can follow the frequency grid; changed acquisition conditions need checking.'})
    for key, label, unit in (('profile_angle', 'Profile direction', '°'), ('profile_speed', 'Profile speed', 'mm/s')):
        before, after = b.get(key), a.get(key)
        if before is not None and after is not None and before != after:
            observations.append({'kind': 'setup', 'title': label + ' changed',
                                 'detail': f'{before:g} → {after:g} {unit}. These are different commanded profile cells.'})
            setup_changes.append(key)

    identity_a, identity_b = measurement_identity(a), measurement_identity(b)
    if identity_a is not None and identity_a == identity_b:
        observations.append({'kind': 'source', 'title': 'Same original recording',
                             'detail': 'Content fingerprints identify the same recorded samples. Renaming or re-importing them does not provide a repeat experiment.'})
    elif a.get('recorded_at') and b.get('recorded_at'):
        observations.append({'kind': 'source', 'title': 'Recording times',
                             'detail': f"Baseline {b['recorded_at']} → current {a['recorded_at']}. Times are inferred from recorder names without timezone verification; baseline/current follow your selection."})

    result['overlay'] = {
        'baseline': {'frequency': b.get('frequency', []), 'raw_psd': b.get('raw_psd', {})},
        'current': {'frequency': a.get('frequency', []), 'raw_psd': a.get('raw_psd', {})},
        'qualification': 'Ordinary recorded acceleration PSD on an absolute scale. ' +
                         ('Acquisition context remains a condition of interpretation.' if check['comparable'] else
                          'Exploratory overlay only: incompatible or unverified direction/acquisition prevents numerical change claims.'),
    }
    result['delta'] = None
    if check['comparable']:
        fa = np.asarray(a['frequency'], dtype=float)
        fb = np.asarray(b['frequency'], dtype=float)
        grid = np.unique(np.concatenate(([low, high], fa[(fa > low) & (fa < high)], fb[(fb > low) & (fb < high)])))
        result['delta'] = {
            'frequency': grid.tolist(),
            'psd': {axis: (np.interp(grid, fa, a['raw_psd'][axis]) - np.interp(grid, fb, b['raw_psd'][axis])).tolist() for axis in AXES},
            'method': 'Signed current minus baseline PSD on the union of native frequency bins inside the selected common band; linear interpolation, no extrapolation and no gain in spectral resolution.',
        }
        spacing = max(_spacing(a), _spacing(b))
        for axis in AXES:
            metrics = result['axes'][axis]
            change = metrics['change_percent']
            if change is None:
                title = f'{axis} band power: percentage unavailable'
            elif abs(change) < .05:
                title = f'{axis} band power changed by less than 0.05%'
            else:
                title = f"{axis} band power {'rose' if change > 0 else 'fell'} by {abs(change):.2f}%"
            observations.append({'kind': 'power', 'title': title, 'band': [low, high],
                                 'detail': f"Power {metrics['before_power']:.5g} → {metrics['after_power']:.5g} (mm/s²)²; band RMS {metrics['before_rms']:.5g} → {metrics['after_rms']:.5g} mm/s². A response change does not establish a cause or whether the machine improved."})
            shifted = [p for p in metrics['peaks']['matched'] if abs(p['shift_hz']) > spacing + 1e-9]
            if shifted:
                text = '; '.join(f"{p['before']['frequency']:.2f} → {p['after']['frequency']:.2f} Hz ({p['shift_hz']:+.2f} Hz)" for p in shifted)
                observations.append({'kind': 'peaks', 'title': f'{axis} matched features moved by more than one frequency bin',
                                     'band': [low, high], 'detail': text + f'. The coarser bin spacing is {spacing:.3g} Hz. Matches are unique nearby candidates, not proof of the same physical mode or statistical significance.'})
            before_unmatched = metrics['peaks']['unmatched_before']
            after_unmatched = metrics['peaks']['unmatched_after']
            if before_unmatched or after_unmatched:
                before_text = ', '.join(f"{p['frequency']:.2f}" for p in before_unmatched) or 'none'
                after_text = ', '.join(f"{p['frequency']:.2f}" for p in after_unmatched) or 'none'
                observations.append({'kind': 'unmatched', 'title': f'{axis} unmatched or ambiguous peak candidates',
                                     'band': [low, high], 'detail': f'Baseline Hz: {before_text}. Current Hz: {after_text}. Detection thresholds and overlapping features can change the candidate list; these are not automatically new/lost modes or a shift of the tallest peak.'})
    else:
        observations.append({'kind': 'caution', 'title': 'Numerical changes withheld',
                             'detail': ' '.join(check['blockers'])})

    observations.extend(_persistence_observations(a, b))

    result['observations'] = observations
    setup_hint = ', '.join(key.replace('_', ' ') for key in setup_changes)
    result['annotation_prompts'] = [
        {'key': 'what_changed', 'label': 'What changed?',
         'prompt': ('Confirm the recorded changes in ' + setup_hint + '. ' if setup_hint else '') +
                   'Record the exact hardware, firmware or test change, when it occurred, and which sensor/mounting, fans, temperature and excitation settings stayed the same.'},
        {'key': 'expected_outcome', 'label': 'What did you expect?',
         'prompt': f'Before interpreting the {low:g}–{high:g} Hz band, record the feature and direction of change you expected. Separate this expectation from what the recordings show.'},
        {'key': 'next_controlled_test', 'label': 'What would test the explanation?',
         'prompt': ('First resolve the listed acquisition, direction or orientation differences. ' if not check['comparable'] else '') +
                   'Repeat unchanged baseline conditions, then change one item and record the outcome. Restore the original condition where practical to check whether the feature returns.'},
    ]
    result['qualification'] = 'Recorded response differences are observations, not a diagnosis, proof of causation, statistical significance or an automatic better/worse verdict.'
    return result
