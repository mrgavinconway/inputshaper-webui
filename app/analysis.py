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
          'klipper': '29d4ec2d6d840ee839333306003a400da186d41d', 'adapter': 2}
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
        if len(measurements) > 512:
            raise ValueError('Maximum 512 measurements per upload.')
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

def analyze_measurement(measurement, settings):
    from .diagnostics import quality, waveform, reduce_spectrogram, peaks, direction
    from shaketune.helpers.spectrogram import compute_spectrogram
    from shaketune.helpers.common_func import compute_mechanical_parameters
    a=measurement['samples']; kind=settings.get('test_type','shaper'); q=quality(a)
    axis=settings.get('axis_override') or direction(measurement['name'])
    if axis=='auto': axis=direction(measurement['name'])
    base={'name':measurement['name'], 'excitation':axis, 'excitation_source':'user' if settings.get('axis_override') not in (None,'','auto') else 'measurement name (inferred)',
          'samples':len(a), 'quality':q, 'waveform':waveform(a) if kind!='vibrations' else None,
          'frequency':[], 'psd':{k:[] for k in ('XYZ','X','Y','Z')}, 'raw_psd':{},
          'filters':[], 'choices':[], 'recommendations':[], 'resonance':None, 'damping':None,
          'damping_status':'unavailable', 'regions':{'relax':None,'warning':None},'peaks':[], 'spectrogram':None,'spectrograms':{}}
    if q['status']=='invalid':return base
    calculator=shaper_calibrate.ShaperCalibrate(printer=None)
    calibration=calculator.process_accelerometer_data(measurement['name'],a)
    mask=calibration.freq_bins<=settings['max_freq']; freq=calibration.freq_bins[mask]
    raw={k:getattr(calibration,v)[mask].copy() for k,v in {'XYZ':'psd_sum','X':'psd_x','Y':'psd_y','Z':'psd_z'}.items()}
    q['resolution_hz']=float(np.median(np.diff(freq)))
    if settings['max_freq']>q['nyquist_hz']:
        q['warnings'].append('Requested frequency range exceeds Nyquist; display is limited to measured bins.');q['status']='review'
    base.update(frequency=freq,raw_psd=raw,diagnostic_peaks={k:peaks(freq,y) for k,y in raw.items()})
    if kind!='vibrations':
        maps={};total=None
        for col,axis_name in enumerate('XYZ',1):
            single=a.copy();single[:,1:]=0;single[:,col]=a[:,col]
            power,times,frequencies=compute_spectrogram(single)
            total=power.copy() if total is None else total+power
            maps[axis_name]=reduce_spectrogram(power,times,frequencies,settings['max_freq'])
        maps['XYZ']=reduce_spectrogram(total,times,frequencies,settings['max_freq'])
        base.update(spectrograms=maps,spectrogram=maps['XYZ'])
    calibration.normalize_to_frequencies()
    base['psd']={k:getattr(calibration,v)[mask] for k,v in {'XYZ':'psd_sum','X':'psd_x','Y':'psd_y','Z':'psd_z'}.items()}
    if kind=='shaper' and max(raw['XYZ'])>0:
        estimated=compute_mechanical_parameters(calibration.psd_sum,calibration.freq_bins)[1]
        r=ShaperComputation([measurement],settings.get('max_smoothing'),settings['scv'],settings['max_freq'],None,None,ENGINE['shaketune'][:12]).compute()
        base.update(filters=r.shaper_table_data['shapers'],choices=r.shaper_choices,recommendations=r.shaper_table_data['recommendations'],
                    resonance=r.fr,damping=r.zeta,damping_status='estimated' if estimated is not None and np.isfinite(estimated) else 'default (estimation failed)',
                    regions={'relax':r.peaks_threshold[0],'warning':r.peaks_threshold[1]},
                    peaks=[{'frequency':f,'power':r.calibration_data.psd_sum[i]} for i,f in zip(r.peaks,r.peaks_freqs)],
                    max_smoothing_computed=r.max_smoothing_computed)
    else:
        base['peaks']=base['diagnostic_peaks']['XYZ']
        base['resonance']=float(freq[np.argmax(raw['XYZ'])]) if max(raw['XYZ']) else None
    return plain(base)


def analyze(path, settings):
    return {'engine':ENGINE,'settings':settings,'measurements':[analyze_measurement(m,settings) for m in load_measurements(path)]}


def analyze_upload(folder, settings):
    from .diagnostics import direction, band_power
    import re
    manifest=folder/'sources.json'
    sources=json.loads(manifest.read_text()) if manifest.exists() else [{'stored':'source.stdata','filename':'source.stdata'}]
    kind=settings.get('test_type','shaper'); result={'engine':ENGINE,'settings':settings,'measurements':[],'test_type':kind}
    originals=[]
    for source in sources:
        for measurement in load_measurements(folder/source['stored']):
            if kind=='shaper' and any(token in measurement['name'].lower() for token in ('staticfreq','vibrations','belts_')):
                raise ValueError('This recording appears to be another test type. Choose its dedicated analysis type in test context.')
            # A filename can supply direction when the inner name has none.
            inner_axis=direction(measurement['name']);file_axis=direction(source['filename'])
            derived=dict(settings)
            confirmed=settings.get('measurement_axes',[])
            explicit=confirmed[len(originals)] if len(confirmed)>len(originals) else 'auto'
            if settings.get('axis_override') in (None,'','auto'):
                derived['axis_override']=inner_axis if inner_axis!='unknown' else file_axis
                if inner_axis!='unknown' and file_axis!='unknown' and inner_axis!=file_axis:derived['axis_override']='unknown'
            if explicit!='auto':derived['axis_override']=explicit
            output=analyze_measurement(measurement,derived)
            output['excitation_source']='user' if explicit!='auto' or settings.get('axis_override') not in (None,'','auto') else 'filename/measurement name (inferred; confirm context)'
            output['source_file']=source['filename']
            if len(sources)>1:output['name']=source['filename']+' / '+output['name']
            result['measurements'].append(output); originals.append(measurement)
            if len(originals)>512:raise ValueError('Maximum 512 measurements per import.')
    if kind=='belts':
        if settings.get('kinematics') not in ('corexy','corexz'):raise ValueError('Belt comparison requires CoreXY or CoreXZ kinematics.')
        if len(originals)!=2:raise ValueError('Belt comparison requires exactly two belt recordings (A and B).')
        if {m['excitation'] for m in result['measurements']}!={'A','B'}:raise ValueError('Belt recordings must identify distinct A and B directions in their names.')
        if any(m['quality']['status']=='invalid' for m in result['measurements']):
            result['belt_analysis']={'error':'Invalid sampling; belt metrics withheld.'}
        else:
            from shaketune.graph_creators.computations.belts_computation import BeltsComputation
            labelled=[{'name':'belt_'+m['excitation'],'samples':o['samples']} for m,o in zip(result['measurements'],originals)]
            r=BeltsComputation(labelled,settings['kinematics'],settings['max_freq'],None,None,ENGINE['shaketune']).compute()
            result['belt_analysis']=plain({'frequency':r.signal1.freqs,'first':r.signal1.psd,'second':r.signal2.psd,
                'first_label':r.signal1_belt,'second_label':r.signal2_belt,'similarity_percent':r.similarity_factor,
                'paired_peaks':r.signal1.paired_peaks,'unpaired_first':r.signal1.unpaired_peaks,'unpaired_second':r.signal2.unpaired_peaks,
                'qualification':'Shape similarity and paired peaks are clues, not a mechanical-health verdict or belt-tension measurement.'})
    if kind=='vibrations':
        cells=[]
        for original,m in zip(originals,result['measurements']):
            match=re.search(r'an(\d+)_([0-9]+)sp(\d+)_([0-9]+)',original['name'])
            if not match:raise ValueError('Vibration-profile recordings must contain angle/speed names such as an45_0sp100_0.')
            angle=float(match[1]+'.'+match[2]);speed=float(match[3]+'.'+match[4])
            power={axis:band_power(m,m['frequency'][0],m['frequency'][-1],axis) for axis in ('XYZ','X','Y','Z')} if m['raw_psd'] else None
            cells.append({'angle':angle,'speed':speed,'power':power,'measurement':len(cells)})
        result['vibration_profile']={'cells':cells,'qualification':'Measured speed/angle cells only; no interpolation into unmeasured directions or automatic fault identification.'}
    return result


if __name__=='__main__':
    source=Path(sys.argv[1]);settings=json.loads(Path(sys.argv[2]).read_text())
    result=analyze_upload(source,settings) if source.is_dir() else analyze(source,settings)
    Path(sys.argv[3]).write_text(json.dumps(result,allow_nan=False))
