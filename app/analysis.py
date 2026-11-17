"""Offline adapter around the pinned, unmodified Shake&Tune engine."""
import io
import json
import os
import sys
from pathlib import Path

import numpy as np
import zstandard as zstd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor'))
os.environ['SHAKETUNE_IN_CLI'] = '1'
os.environ['MPLBACKEND'] = 'Agg'
from extras import shaper_calibrate, shaper_defs
sys.modules['shaper_calibrate'] = shaper_calibrate
sys.modules['shaper_defs'] = shaper_defs
from shaketune.graph_creators.computations.shaper_computation import ShaperComputation

ENGINE = {'shaketune': '354ee4be6db218715239f7b9411c2c37fdea23dd',
          'klipper': '29d4ec2d6d840ee839333306003a400da186d41d', 'adapter': 1}
MAX_DECOMPRESSED = 256 * 1024 * 1024

def load_measurements(path):
    with open(path, 'rb') as f:
        if f.read(4) != b'\x28\xb5\x2f\xfd':
            raise ValueError('Expected modern Zstandard JSON .stdata. Legacy pickle files are not supported; export with Shake&Tune 5.1 or newer.')
        f.seek(0)
        with zstd.ZstdDecompressor().stream_reader(f) as reader:
            raw = reader.read(MAX_DECOMPRESSED + 1)
    if len(raw) > MAX_DECOMPRESSED:
        raise ValueError('Decompressed file exceeds 256 MiB limit.')
    measurements = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = json.loads(line)
        a = np.asarray(m.get('samples'), dtype=float)
        if a.ndim != 2 or a.shape[1] != 4 or len(a) < 1024 or len(a) > 2_000_000:
            raise ValueError('Each measurement must contain 1,024–2,000,000 samples of time, X, Y, Z.')
        if not np.isfinite(a).all() or not np.all(np.diff(a[:, 0]) > 0):
            raise ValueError('Samples must be finite and timestamps strictly increasing.')
        if a[-1, 0] - a[0, 0] < 1:
            raise ValueError('Recording must span at least one second.')
        rate = len(a) / (a[-1, 0] - a[0, 0])
        if not 100 <= rate <= 100000:
            raise ValueError('Sample rate is outside the supported range.')
        measurements.append({'name': str(m.get('name', 'Measurement'))[:200], 'samples': a})
        if len(measurements) > 12:
            raise ValueError('Maximum 12 measurements per upload.')
    if not measurements:
        raise ValueError('No measurements found.')
    return measurements

def plain(value):
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, np.generic):
        return plain(value.item())
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value

def analyze(path, settings):
    results = []
    for measurement in load_measurements(path):
        r = ShaperComputation([measurement], settings.get('max_smoothing'),
            settings['scv'], settings['max_freq'], None, None, ENGINE['shaketune'][:12]).compute()
        c = r.calibration_data
        fi = np.flatnonzero(r.t <= settings['max_freq'])
        # Shake&Tune result uses t for frequency and bins for time (matching its plotter).
        ti = np.arange(0, len(r.bins), max(1, int(np.ceil(len(r.bins) / 500))))
        fi = fi[::max(1, int(np.ceil(len(fi) / 500)))]
        power = r.pdata[np.ix_(fi, ti)]
        results.append(plain({'name': measurement['name'], 'frequency': c.freqs,
            'psd': {'XYZ': c.psd_sum, 'X': c.psd_x, 'Y': c.psd_y, 'Z': c.psd_z},
            'filters': r.shaper_table_data['shapers'], 'recommendations': r.shaper_table_data['recommendations'],
            'choices': r.shaper_choices, 'damping': r.zeta, 'resonance': r.fr,
            'regions': {'relax': r.peaks_threshold[0], 'warning': r.peaks_threshold[1]},
            'peaks': [{'frequency': f, 'power': c.psd_sum[i]} for i, f in zip(r.peaks, r.peaks_freqs)],
            'spectrogram': {'frequency': r.t[fi], 'time': r.bins[ti],
                            'power_db': (10 * np.log10(np.maximum(power, 1e-20))).T},
            'samples': len(measurement['samples']), 'max_smoothing_computed': r.max_smoothing_computed}))
    return {'engine': ENGINE, 'settings': settings, 'measurements': results}

if __name__ == '__main__':
    result = analyze(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()))
    Path(sys.argv[3]).write_text(json.dumps(result, allow_nan=False))
