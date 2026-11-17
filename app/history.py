"""Compact, source-linked history for exploratory resonance comparisons."""
import json
import math
import uuid
from pathlib import Path

from .archive_import import filename_info
from .diagnostics import band_power
from .result_summary import summarize_result, source_digest, measurement_identity


def write_summary(folder, result):
    folder = Path(folder)
    summary = summarize_result(result)
    target = folder / 'summary.json'
    temporary = folder / ('summary-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(summary, allow_nan=False), encoding='utf-8')
        temporary.replace(target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return summary


def _valid_summary(summary):
    return (isinstance(summary, dict) and summary.get('summary_version') == 1
            and isinstance(summary.get('settings'), dict)
            and isinstance(summary.get('engine'), dict)
            and isinstance(summary.get('measurements'), list)
            and all(isinstance(m, dict) and isinstance(m.get('name'), str)
                    and type(m.get('measurement')) is int and m['measurement'] == index
                    and isinstance(m.get('frequency', []), list)
                    and isinstance(m.get('raw_psd', {}), dict)
                    for index, m in enumerate(summary['measurements'])))


def read_summary(folder):
    folder = Path(folder)
    target = folder / 'summary.json'
    result_path = folder / 'result.json'
    try:
        if target.exists() and target.stat().st_mtime_ns >= result_path.stat().st_mtime_ns:
            cached = json.loads(target.read_text(encoding='utf-8'))
            if _valid_summary(cached):
                return cached
    except (OSError, ValueError):
        pass
    result = json.loads(result_path.read_text(encoding='utf-8'))
    manifest = folder / 'sources.json'
    try:
        sources = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else [{'stored':'source.stdata','filename':'source.stdata'}]
        if not isinstance(sources, list):
            sources = []
        sources = [source for source in sources if isinstance(source, dict)
                   and isinstance(source.get('stored'), str) and isinstance(source.get('filename'), str)]
    except (OSError, ValueError):
        sources = []
    # Legacy results gain original-file fingerprints without recomputing spectra.
    per_source_count = {}
    for measurement in result.get('measurements', []):
        source = next((s for s in sources if s['filename'] == measurement.get('source_file')), sources[0] if len(sources) == 1 else None)
        if source:
            index = per_source_count.get(source['stored'], 0)
            per_source_count[source['stored']] = index + 1
            measurement.setdefault('source_file', source['filename'])
            source_path = folder / source['stored']
            try:
                if source.get('sha256') or source_path.is_file():
                    measurement.setdefault('source_sha256', source.get('sha256') or source_digest(source_path))
            except OSError:
                pass
            measurement.setdefault('source_measurement_index', index)
    summary = summarize_result(result)
    try:
        write_summary(folder, result)
    except (OSError, ValueError, TypeError):
        # Index persistence is optional: a full/read-only disk must not make
        # readable, authoritative result files disappear from the library.
        pass
    return summary


def recording_keys(measurement):
    keys = set()
    identity = measurement_identity(measurement)
    if identity:
        keys.add(identity)
    source = measurement.get('source_sha256')
    if source and measurement.get('source_measurement_index') is not None:
        keys.add(('source', source, measurement['source_measurement_index']))
    return keys


def cohort(record):
    settings = record.get('settings', {})
    rate = record.get('quality', {}).get('mean_sample_rate_hz') or record.get('quality', {}).get('sample_rate_hz')
    bucket = str(round(rate / 50) * 50) if rate and math.isfinite(rate) else 'unknown'
    profile = record.get('signal_profile', {})
    orientation = profile.get('dominant_dc_axis', 'unknown') if profile.get('gravity_plausible') else 'unknown'
    sensor = settings.get('sensor') or record.get('source_label') or 'unlabelled'
    mount = settings.get('sensor_mount') or 'mount unknown'
    mapping = settings.get('axes_map') or 'mapping unknown'
    key = '|'.join((settings.get('acquisition_group') or '', str(sensor), bucket, orientation, mount, mapping))
    return key, f'{sensor} · ~{bucket} samples/s · apparent {orientation}'


def build_records(data, rows):
    records = []
    identities = {}
    errors = []
    for row in rows:
        try:
            summary = read_summary(Path(data) / row['id'])
        except (OSError, ValueError, KeyError) as exc:
            errors.append({'run_id': row['id'], 'message': 'History unavailable: ' + str(exc)[:200]})
            continue
        for measurement in summary['measurements']:
            info = filename_info(measurement.get('source_file', row.get('filename', '')))
            record = {**measurement, 'run_id': row['id'], 'title': row['title'],
                      'friendly_name': row.get('friendly_name') or '',
                      'recorded_at': measurement.get('recorded_at') or info['recorded_at'],
                      'recorded_at_source': measurement.get('recorded_at_source') or ('filename; timezone unknown' if info['recorded_at'] else 'unknown'),
                      'source_label': info['source_label'], 'settings': summary['settings'],
                      'engine': summary['engine'], 'test_type': summary['test_type'],
                      'imported_at': row['created'], 'duplicate_count': 1}
            record['cohort_key'], record['cohort_label'] = cohort(record)
            keys = recording_keys(record)
            existing = next((identities[key] for key in keys if key in identities), None)
            if existing is not None:
                existing['duplicate_count'] += 1
                continue
            records.append(record)
            for key in keys:
                identities[key] = record
    records.sort(key=lambda r: (r.get('recorded_at') or '', r['source_file'] if 'source_file' in r else r['name']))
    return records, errors


def band_metrics(record, low, high, axis):
    try:
        values = {key: band_power(record, low, high, key) for key in ('XYZ', 'X', 'Y', 'Z')}
    except ValueError as exc:
        return {'run_id': record['run_id'], 'measurement': record['measurement'], 'error': str(exc)}
    total = values['XYZ']
    return {'run_id': record['run_id'], 'measurement': record['measurement'],
            'power': values[axis], 'rms': math.sqrt(max(0, values[axis])),
            'shares': {key: values[key] / total if total > 0 else None for key in 'XYZ'},
            'powers': values, 'peaks': [p for p in record.get('diagnostic_peaks', {}).get(axis, []) if low <= p['frequency'] <= high],
            'quality': record.get('quality'), 'cohort_key': record.get('cohort_key')}
