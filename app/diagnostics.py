"""Measurement quality and conservative comparisons. No component diagnosis from a peak."""
import re
import numpy as np

CONTEXT = ('printer', 'test_type', 'test_mode', 'sensor', 'sensor_mount', 'axes_map',
           'position', 'accel_per_hz', 'sweep_rate', 'fans', 'temperature', 'kinematics',
           'acquisition_group', 'umbilical', 'intervention')

# Screening limits, not sensor specifications. Sparse dropped samples are retained
# with a caveat; no interpolation or repair is silently applied to the recordings.
MAX_REVIEW_GAP_PERIODS = 3.0
MAX_REVIEW_MISSING_FRACTION = .001


def signal_profile(a):
    """Record DC and timing evidence without claiming a calibrated sensor pose."""
    dc = np.mean(a[:, 1:4], axis=0)
    norm = float(np.linalg.norm(dc))
    unit = dc / norm if norm > 0 else np.zeros(3)
    dominant = int(np.argmax(np.abs(unit)))
    intervals = np.diff(a[:, 0])
    return {
        'dc_acceleration': dict(zip('XYZ', map(float, dc))),
        'apparent_gravity_norm': norm,
        'gravity_direction': dict(zip('XYZ', map(float, unit))),
        'dominant_dc_axis': ('+' if unit[dominant] >= 0 else '-') + 'XYZ'[dominant] if norm > 0 else 'unknown',
        'gravity_plausible': 6000 <= norm <= 14000,
        'sample_timing': {'median_interval_s': float(np.median(intervals)),
                          'mean_interval_s': float(np.mean(intervals)),
                          'p01_interval_s': float(np.percentile(intervals, 1)),
                          'p99_interval_s': float(np.percentile(intervals, 99))},
        'qualification': 'Mean recorded acceleration in assumed mm/s²; apparent gravity is an orientation/mapping clue, not a calibrated pose or sensor identification.',
    }

def direction(name):
    axes = set(re.findall(r'(?:^|[_\W])([XYZAB])(?=[_\W]|$)', name.upper()))
    return next(iter(axes)) if len(axes) == 1 else 'unknown'

def quality(a):
    dt = np.diff(a[:, 0]); median = np.median(dt)
    jitter = float(np.percentile(abs(dt / median - 1), 99))
    periods = dt / median
    gap_mask = periods > 1.5
    gaps = int(np.sum(gap_mask))
    missing = int(np.maximum(np.rint(periods[gap_mask]).astype(np.int64) - 1, 0).sum())
    missing_fraction = missing / (len(a) + missing)
    max_gap_periods = float(periods.max())
    # Timestamp subtraction can put an exact three-period gap a few ulps over
    # the threshold; this tolerance is far below a meaningful timing difference.
    invalid = jitter > .2 + 1e-8 or max_gap_periods > MAX_REVIEW_GAP_PERIODS + 1e-6 or missing_fraction > MAX_REVIEW_MISSING_FRACTION
    warnings = []
    if invalid:
        warnings.append('Irregular sampling: spectra and shaper fitting are withheld. Inspect the waveform and repeat the recording.')
    elif gaps:
        warnings.append('Sparse short sampling gaps: spectra retain the original samples using the engine’s uniform-sampling approximation. Treat small changes cautiously; no samples were repaired.')
    flat = []
    for i, axis in enumerate('XYZ', 1):
        y = a[:, i]
        if np.ptp(y) == 0:
            warnings.append(f'{axis} channel is constant; check sensor/configuration.')
        elif max(np.mean(y == y.min()), np.mean(y == y.max())) > .01:
            flat.append(axis)
    if flat:
        warnings.append('Repeated extrema on '+', '.join(flat)+': possible clipping/quantisation. Sensor limits are unknown; inspect the waveform.')
    return {'status': 'invalid' if invalid else ('review' if warnings else 'pass'),
            'warnings': warnings, 'sample_rate_hz': float(1 / median), 'duration_s': float(a[-1, 0]-a[0, 0]),
            'largest_gap_ms': float(dt.max()*1000), 'gap_count': gaps,
            'largest_gap_periods': max_gap_periods, 'estimated_missing_samples': missing,
            'estimated_missing_fraction': missing_fraction,
            'mean_sample_rate_hz': float((len(a)-1)/(a[-1,0]-a[0,0])),
            'sampling_policy': {'max_review_gap_periods': MAX_REVIEW_GAP_PERIODS,
                                'max_review_missing_fraction': MAX_REVIEW_MISSING_FRACTION,
                                'max_review_p99_jitter_percent': 20,
                                'qualification': 'Heuristic screening limits; spectra use original samples without resampling.'},
            'timing_jitter_p99_percent': jitter*100, 'nyquist_hz': float(.5/median),
            'sensor_limit': 'unknown'}

def waveform(a):
    # Union of per-axis extrema preserves short events rather than skipping samples.
    groups = np.array_split(np.arange(len(a)), min(600, len(a)))
    indices = sorted({int(i) for g in groups for col in (1,2,3)
                      for i in (g[np.argmin(a[g,col])],g[np.argmax(a[g,col])])})
    return {'time': (a[indices,0]-a[0,0]).tolist(),
            **{axis:a[indices,i].tolist() for i,axis in enumerate('XYZ',1)},
            'reduction':'per-axis min/max envelope; quantitative analysis uses original samples'}

def reduce_spectrogram(power, times, freqs, max_freq):
    keep = freqs <= max_freq
    p = power[keep]; f = freqs[keep]
    fg = np.array_split(np.arange(len(f)), min(300,len(f)))
    tg = np.array_split(np.arange(len(times)), min(300,len(times)))
    # Max pooling retains narrow/brief features. Display only: never used for metrics.
    compressed = np.array([[p[np.ix_(x,y)].max() for x in fg] for y in tg])
    return {'frequency':[float(f[g].mean()) for g in fg], 'time':[float(times[g].mean()) for g in tg],
            'power_db':(10*np.log10(np.maximum(compressed,1e-20))).tolist(),
            'reduction':'maximum power per display cell; not averaged power'}

def peaks(freq, power):
    f=np.asarray(freq); y=np.asarray(power)
    if len(f)<3 or y.max()<=0:return []
    noise=float(np.median(y)); threshold=max(noise*5, y.max()*.01)
    idx=np.where((y[1:-1]>y[:-2]) & (y[1:-1]>=y[2:]) & (y[1:-1]>threshold))[0]+1
    result=[]
    for i in sorted(idx, key=lambda i:y[i], reverse=True)[:30]:
        half=noise+(y[i]-noise)/2; left=i; right=i
        while left>0 and y[left]>half:left-=1
        while right<len(y)-1 and y[right]>half:right+=1
        width=None
        if y[left]<=half and y[right]<=half:
            lo=np.interp(half,[y[left],y[left+1]],[f[left],f[left+1]])
            hi=np.interp(half,[y[right],y[right-1]],[f[right],f[right-1]])
            width=float(hi-lo)
        result.append({'frequency':float(f[i]),'power':float(y[i]),'width_hz':width,
                       'prominence_above_median':float(y[i]-noise)})
    return sorted(result,key=lambda p:p['frequency'])

def match_peaks(before, after, tolerance=5):
    # Only unique mutual candidates: ambiguous clusters remain unmatched.
    matches=[]; used_b=set(); used_a=set()
    for i,b in enumerate(before):
        candidates=[j for j,a in enumerate(after) if abs(a['frequency']-b['frequency'])<=tolerance]
        if len(candidates)!=1:continue
        j=candidates[0]; a=after[j]
        if sum(abs(x['frequency']-a['frequency'])<=tolerance for x in before)!=1:continue
        used_b.add(i);used_a.add(j)
        matches.append({'before':b,'after':a,'shift_hz':a['frequency']-b['frequency'],
                        'power_change_percent':100*(a['power']/b['power']-1) if b['power'] else None})
    return {'matched':matches,'unmatched_before':[b for i,b in enumerate(before) if i not in used_b],
            'unmatched_after':[a for i,a in enumerate(after) if i not in used_a],
            'tolerance_hz':tolerance,'method':'unique frequency-neighbour candidates; not proof of the same physical mode'}

def compatibility(a,b,sa,sb,ea=None,eb=None):
    blockers=[]; warnings=[]
    if a.get('excitation','unknown')=='unknown' or b.get('excitation','unknown')=='unknown':
        blockers.append('Excitation direction is unknown; confirm it in test context.')
    elif a['excitation']!=b['excitation']:
        blockers.append('Different excitation directions: exploratory overlay only.')
    if a.get('quality',{}).get('status')=='invalid' or b.get('quality',{}).get('status')=='invalid':
        blockers.append('Invalid sampling in one recording.')
    for label,m in (('current',a),('baseline',b)):
        warnings.extend(label+': '+w for w in m.get('quality',{}).get('warnings',[]))
    if not a.get('raw_psd') or not b.get('raw_psd'):
        blockers.append('Re-analyse older imports to enable diagnostic metrics.')
    for key in CONTEXT:
        av=sa.get(key);bv=sb.get(key)
        if av in (None,'','unknown','auto') or bv in (None,'','unknown','auto'):
            warnings.append(f'{key}: missing/unknown context')
        elif av!=bv:
            warnings.append(f'{key}: differs ({av} → {bv})')
            if key in ('printer','test_type','test_mode','sensor','sensor_mount','axes_map','accel_per_hz','sweep_rate','kinematics','acquisition_group'):
                blockers.append(f'{key} differs; acquisition conditions are not equivalent.')
    extra_fields = ['sweep_start_hz', 'sweep_end_hz', 'sweep_duration_s', 'sweep_profile', 'acceleration_units']
    if sa.get('test_type') == 'static' or sb.get('test_type') == 'static':
        extra_fields.append('static_frequency')
    for key in extra_fields:
        av=sa.get(key); bv=sb.get(key)
        if av in (None,'','unknown','auto') or bv in (None,'','unknown','auto'):
            if av is not None or bv is not None or key == 'static_frequency':
                warnings.append(f'{key}: missing/unknown context')
        elif av != bv:
            blockers.append(f'{key} differs; acquisition conditions are not equivalent.')
    # These parameters are measurement-specific for constant-speed profiles.
    for key in ('profile_angle', 'profile_speed'):
        av=a.get(key); bv=b.get(key)
        if av is not None and bv is not None and av != bv:
            blockers.append(f'{key} differs; these are different measured profile cells.')
    for key,label in (('sample_rate_hz','Sample rate'), ('resolution_hz','Frequency-bin spacing')):
        av=a.get('quality',{}).get(key); bv=b.get('quality',{}).get(key)
        if av is None or bv is None or av <= 0 or bv <= 0:
            warnings.append(f'{label}: unrecorded; acquisition equivalence is unverified.')
            continue
        relative=abs(av-bv)/max(av,bv)
        if relative > .02:
            blockers.append(f'{label} differs by more than 2%; acquisition or spectral resolution changed.')
        elif relative > .001:
            warnings.append(f'{label} differs slightly ({bv:.4g} → {av:.4g}); small peak shifts may reflect sampling-clock or frequency-bin changes.')
    pa=a.get('signal_profile',{}); pb=b.get('signal_profile',{})
    orientation_change=None
    if pa.get('gravity_plausible') and pb.get('gravity_plausible'):
        da=np.array([pa.get('gravity_direction',{}).get(k,0) for k in 'XYZ'])
        db=np.array([pb.get('gravity_direction',{}).get(k,0) for k in 'XYZ'])
        if np.linalg.norm(da)>0 and np.linalg.norm(db)>0:
            orientation_change=float(np.degrees(np.arccos(np.clip(np.dot(da,db)/(np.linalg.norm(da)*np.linalg.norm(db)),-1,1))))
            if orientation_change > 15:
                blockers.append('Apparent-gravity direction changed by more than 15°; verify sensor orientation, mounting and axes mapping before comparing axis powers.')
    if ea!=eb:blockers.append('Analysis engine versions differ; re-analyse with the same version.')
    return {'comparable':not blockers,'blockers':blockers,'warnings':warnings,
            'orientation_change_degrees':orientation_change,
            'qualification':'conditional on missing context' if warnings else 'recorded context matches'}

def band_power(m,low,high,axis):
    if m.get('quality',{}).get('status') == 'invalid':
        raise ValueError('Spectral metrics are unavailable because this recording has invalid sampling.')
    try:
        f=np.asarray(m['frequency'], dtype=float)
        p=np.asarray(m['raw_psd'][axis], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Ordinary PSD is unavailable; re-analyse this recording to enable band metrics.') from exc
    if (f.ndim != 1 or p.ndim != 1 or len(f) < 2 or len(f) != len(p)
            or not np.isfinite(f).all() or not np.isfinite(p).all()
            or not np.all(np.diff(f) > 0) or f[0] < 0 or np.any(p < 0)):
        raise ValueError('The recorded spectrum is missing or invalid; re-analyse this recording.')
    if not np.isfinite([low,high]).all() or low<f[0] or high>f[-1] or high<=low:
        raise ValueError('Select a non-empty band inside the common measured frequency range.')
    x=np.r_[low,f[(f>low)&(f<high)],high]
    if high-low<np.median(np.diff(f)):
        raise ValueError('Selected band is narrower than the spectral resolution.')
    return float(np.trapezoid(np.interp(x,f,p),x))

def compare(a,b,sa,sb,low,high,ea=None,eb=None):
    check=compatibility(a,b,sa,sb,ea,eb)
    result={'compatibility':check,'axes':{},'band':[low,high]}
    if not check['comparable']:return result
    for axis in ('XYZ','X','Y','Z'):
        before=band_power(b,low,high,axis);after=band_power(a,low,high,axis)
        result['axes'][axis]={'before_power':before,'after_power':after,
            'before_rms':float(np.sqrt(before)),'after_rms':float(np.sqrt(after)),
            'change_percent':100*(after/before-1) if before>0 else None,
            'peaks':match_peaks([p for p in b.get('diagnostic_peaks',{}).get(axis,[]) if low<=p['frequency']<=high],
                                [p for p in a.get('diagnostic_peaks',{}).get(axis,[]) if low<=p['frequency']<=high])}
    return result
