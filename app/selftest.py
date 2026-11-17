"""Small real-engine smoke test, run before release activation."""
import json
import tempfile
from pathlib import Path
import numpy as np
import zstandard as zstd
from .analysis import analyze

def fixture(path):
    rng = np.random.default_rng(42)
    t = np.arange(0, 4, 1 / 1000)
    a = np.sin(2 * np.pi * 42 * t) * 1500 + rng.normal(0, 30, len(t))
    samples = np.column_stack((t, a * .1, a, a * .3))
    path.write_bytes(zstd.ZstdCompressor().compress((json.dumps({'name':'synthetic_validation_Y', 'samples':samples.tolist()})+'\n').encode()))

def main():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'test.stdata'
        fixture(p)
        result = analyze(p, {'scv': 5, 'max_freq': 200, 'max_smoothing': None})
        m = result['measurements'][0]
        assert len(m['filters']) == 5
        assert abs(m['resonance'] - 42) < 4
        assert abs(m['regions']['relax'] - max(m['psd']['XYZ']) * .05) < 1e-5
        assert abs(m['regions']['warning'] - max(m['psd']['XYZ']) * .12) < 1e-5
        np.testing.assert_allclose(np.array(m['psd']['XYZ']), np.array(m['psd']['X']) + m['psd']['Y'] + np.array(m['psd']['Z']))
        assert np.asarray(m['spectrogram']['power_db']).shape == (len(m['spectrogram']['time']), len(m['spectrogram']['frequency']))
        assert all(len(f['vals']) == len(m['frequency']) for f in m['filters'])
        print('Real-engine smoke test passed')

if __name__ == '__main__':
    main()
