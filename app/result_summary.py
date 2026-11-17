"""Small archive records and exact recording identities, independent of plot data."""
import hashlib
import re
from pathlib import Path

import numpy as np


def source_digest(path):
    """Hash original file bytes without loading the compressed file into memory."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def measurement_digest(samples):
    """Identify identical samples even after renaming, recompression or JSON export.

    Absolute timestamps are retained: a later recording of an identical signal is
    still a separate recording. This identifies duplicates, not statistical independence.
    """
    values = np.ascontiguousarray(samples, dtype='<f8')
    if values.ndim != 2 or values.shape[1] != 4 or not np.isfinite(values).all():
        raise ValueError('A recording fingerprint requires finite time/X/Y/Z samples.')
    digest = hashlib.sha256(b'inputshaper-samples-f64le-v1\0')
    digest.update(str(values.shape).encode('ascii'))
    digest.update(memoryview(values).cast('B'))
    return digest.hexdigest()


def measurement_identity(measurement):
    """Return an exact duplicate key, or None for older records without hashes."""
    for key in ('measurement_sha256', 'source_sha256'):
        value = measurement.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', value):
            continue
        if key == 'measurement_sha256':
            return ('measurement', value.lower())
        index = measurement.get('source_measurement_index')
        if type(index) is int and index >= 0:
            return ('source', value.lower(), index)
    return None


def summarize_result(result):
    """Keep spectra/history evidence while omitting large time maps and waveforms.

    Frequency and raw PSD arrays remain full resolution (normally a few hundred
    bins), allowing honest band comparisons without re-reading full result files.
    The summary is an index/cache; originals and the full result remain authoritative.
    """
    measurements = []
    retained = (
        'name', 'source_file', 'source_sha256', 'source_measurement_index',
        'measurement_sha256', 'recorded_at', 'recorded_at_source',
        'excitation', 'excitation_source', 'samples', 'quality', 'signal_profile', 'spectral_features',
        'frequency', 'raw_psd', 'diagnostic_peaks', 'resonance', 'damping',
        'damping_status', 'damping_method', 'choices', 'profile_angle', 'profile_speed', 'context', 'provenance',
    )
    for index, measurement in enumerate(result.get('measurements', [])):
        summary = {key: measurement[key] for key in retained if key in measurement}
        summary['measurement'] = index
        summary['filters'] = [
            {key: value for key, value in entry.items() if key not in ('vals', 'freq_bins')}
            for entry in measurement.get('filters', [])
        ]
        summary['recommendations'] = measurement.get('recommendations', [])
        measurements.append(summary)
    return {
        'summary_version': 2,
        'engine': result.get('engine', {}),
        'settings': result.get('settings', {}),
        'test_type': result.get('test_type', result.get('settings', {}).get('test_type', 'shaper')),
        'measurements': measurements,
    }
