"""Regressions from independent review of archive/history failure paths."""
import io
import gzip
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from app import archive_import, history, main
from app.changes import summarize_changes
from app.diagnostics import band_power


def spectrum(identity='a'):
    return {'name': 'axis_X', 'excitation': 'X', 'frequency': [0, 10, 20, 30],
            'raw_psd': {axis: [0, 1, 2, 0] for axis in ('XYZ', 'X', 'Y', 'Z')},
            'quality': {'status': 'pass', 'sample_rate_hz': 3300, 'resolution_hz': 10},
            'measurement_sha256': identity * 64}


def result(measurement, settings=None):
    return {'settings': {'test_type': 'shaper', **(settings or {})},
            'engine': {'adapter': 3}, 'measurements': [measurement]}


def save_record(measurement, settings=None):
    run_id = uuid.uuid4().hex
    folder = main.DATA / run_id
    folder.mkdir()
    data = result(measurement, settings)
    (folder / 'result.json').write_text(json.dumps(data), encoding='utf-8')
    history.write_summary(folder, data)
    with main.db() as connection:
        connection.execute('INSERT INTO runs(id,title,filename,created,status,settings) VALUES(?,?,?,?,?,?)',
                           (run_id, 'Review', 'original.stdata', time.time(), 'ready', json.dumps(data['settings'])))
    return {'run_id': run_id, 'measurement': 0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    with TestClient(main.app) as connected:
        assert connected.post('/api/login', json={'password': 'test-password'}).status_code == 200
        yield connected


def test_archive_caps_pax_metadata_before_tar_parser_runs(tmp_path, monkeypatch):
    path = tmp_path / 'metadata.tgz'
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT,
                      pax_headers={'comment': 'x' * 20000}) as archive:
        payload = b'\x28\xb5\x2f\xfd' + b'data'
        member = tarfile.TarInfo('axis_X_20261117_120000.stdata')
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    assert path.stat().st_size < 1024
    monkeypatch.setattr(archive_import, 'MAX_BATCH_BYTES', 16384)

    def forbid_tar_parser(*args, **kwargs):
        raise AssertionError('Oversized expanded metadata must never reach tarfile')

    monkeypatch.setattr(archive_import.tarfile, 'open', forbid_tar_parser)
    with pytest.raises(ValueError, match='including metadata'):
        archive_import.stage_archive(path, tmp_path / 'staged')
    assert not list((tmp_path / 'staged').glob('*.stdata'))


def test_archive_rejects_forged_metadata_size_before_allocating(tmp_path):
    header = tarfile.TarInfo('long-name')
    header.type = tarfile.GNUTYPE_LONGNAME
    header.size = 2 ** 40
    path = tmp_path / 'forged.tgz'
    path.write_bytes(gzip.compress(header.tobuf(format=tarfile.GNU_FORMAT) + b'\0' * 1024))
    with pytest.raises(ValueError, match='metadata header exceeds'):
        archive_import.stage_archive(path, tmp_path / 'staged')


def test_archive_rejects_recursive_metadata_chain(tmp_path):
    header = tarfile.TarInfo('pax')
    header.type = tarfile.XHDTYPE
    header.size = 0
    path = tmp_path / 'nested.tgz'
    path.write_bytes(gzip.compress(header.tobuf() * 40 + b'\0' * 1024))
    with pytest.raises(ValueError, match='nested too deeply'):
        archive_import.stage_archive(path, tmp_path / 'staged')


@pytest.mark.parametrize('invalid', [True, False])
def test_unavailable_history_record_does_not_discard_healthy_band_metrics(client, invalid):
    healthy = save_record(spectrum())
    unavailable = spectrum('b')
    unavailable['frequency'] = []
    unavailable['raw_psd'] = {}
    if invalid:
        unavailable['quality']['status'] = 'invalid'
    else:
        unavailable.pop('raw_psd')  # Legacy result without ordinary PSD.
    other = save_record(unavailable)
    response = client.post('/api/history/band', json={
        'references': [healthy, other], 'low': 0, 'high': 20, 'axis': 'XYZ'})
    assert response.status_code == 200, response.text
    metrics = response.json()['records']
    assert metrics[0]['power'] == pytest.approx(20)
    assert metrics[1]['error']
    assert 'power' not in metrics[1]


@pytest.mark.parametrize('frequencies,powers', [
    ([0, 10, 10, 30], [0, 1, 2, 0]),
    ([0, 20, 10, 30], [0, 1, 2, 0]),
    ([0, 10, 20, 30], [0, 1, 2]),
    ([0, 10, 20, 30], [0, float('nan'), 2, 0]),
    ([0, 10, 20, 30], [0, -1, 2, 0]),
])
def test_band_metrics_reject_invalid_spectral_arrays(frequencies, powers):
    measurement = spectrum()
    measurement['frequency'] = frequencies
    measurement['raw_psd']['XYZ'] = powers
    with pytest.raises(ValueError, match='invalid'):
        band_power(measurement, 0, 20, 'XYZ')


@pytest.mark.parametrize('field', ['umbilical', 'intervention', 'acquisition_group'])
def test_recorded_condition_changes_are_not_untouched_repeats(client, field):
    baseline = save_record(spectrum('a'), {field: 'original'})
    current = save_record(spectrum('b'), {field: 'original'})
    changed = save_record(spectrum('c'), {field: 'changed'})
    response = client.post('/api/compare', json={
        'baseline': baseline, 'current': current, 'repeats': [changed], 'low': 0, 'high': 20})
    assert response.status_code == 200, response.text
    repeats = response.json()['repeatability']
    assert repeats['count'] == 1
    assert repeats['axes'] == {}
    assert any(field in reason for reason in repeats['excluded'][0]['reason'])


@pytest.mark.parametrize('field', ['umbilical', 'intervention', 'acquisition_group'])
def test_recorded_setup_changes_are_visible_in_comparison_observations(field):
    data = summarize_changes(spectrum('b'), spectrum('a'), {field: 'changed'}, {field: 'original'},
                             {'adapter': 3}, {'adapter': 3}, 0, 20)
    assert any(observation['kind'] == 'setup' and field.replace('_', ' ').capitalize() in observation['title']
               for observation in data['observations'])
    assert data['compatibility']['comparable'] is (field != 'acquisition_group')
    assert field.replace('_', ' ') in data['annotation_prompts'][0]['prompt']


@pytest.mark.parametrize('contents', ['{', '{"summary_version":0}',
                                    '{"summary_version":1,"settings":{},"engine":{},"measurements":"invalid"}'])
def test_corrupt_or_old_cache_is_rebuilt_from_authoritative_result(tmp_path, contents):
    data = result(spectrum())
    (tmp_path / 'result.json').write_text(json.dumps(data), encoding='utf-8')
    (tmp_path / 'summary.json').write_text(contents, encoding='utf-8')
    summary = history.read_summary(tmp_path)
    assert summary['summary_version'] == 1
    assert summary['measurements'][0]['name'] == 'axis_X'
    assert json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8')) == summary


def test_failed_cache_persistence_does_not_hide_existing_result(client, monkeypatch):
    ref = save_record(spectrum())
    folder = main.DATA / ref['run_id']
    (folder / 'summary.json').unlink()

    def fail_write(*args, **kwargs):
        raise PermissionError('Simulated full or read-only cache storage')

    monkeypatch.setattr(history, 'write_summary', fail_write)
    response = client.get('/api/catalog')
    assert response.status_code == 200, response.text
    assert response.json()[0]['run_id'] == ref['run_id']
    response = client.get('/api/history')
    assert response.status_code == 200, response.text
    assert response.json()['records'][0]['run_id'] == ref['run_id']
    assert response.json()['errors'] == []


def test_cache_failure_does_not_misreport_successful_reanalysis(client, monkeypatch):
    ref = save_record(spectrum(), {'umbilical': 'original'})
    folder = main.DATA / ref['run_id']
    previous = (folder / 'result.json').read_bytes()
    new_result = result(spectrum('b'), {'umbilical': 'changed'})

    def finished_analysis(arguments, **kwargs):
        Path(arguments[-1]).write_text(json.dumps(new_result), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    def fail_cache(*args, **kwargs):
        raise OSError('Simulated cache write failure')

    monkeypatch.setattr(main.subprocess, 'run', finished_analysis)
    monkeypatch.setattr(main, 'write_summary', fail_cache)
    main.process(ref['run_id'])
    row = main.row_run(ref['run_id'])
    assert row['status'] == 'ready' and row['error'] is None
    assert json.loads((folder / 'result.json').read_text()) == new_result
    assert next((folder / 'history').glob('*.json')).read_bytes() == previous


def test_failed_atomic_cache_replace_preserves_previous_cache(tmp_path, monkeypatch):
    history.write_summary(tmp_path, result(spectrum()))
    prior = (tmp_path / 'summary.json').read_bytes()
    real_replace = Path.replace

    def refuse_cache_replace(path, target):
        if path.name.startswith('summary-'):
            raise OSError('Simulated replace failure')
        return real_replace(path, target)

    monkeypatch.setattr(Path, 'replace', refuse_cache_replace)
    with pytest.raises(OSError, match='replace failure'):
        history.write_summary(tmp_path, result(spectrum('b')))
    assert (tmp_path / 'summary.json').read_bytes() == prior
    assert not list(tmp_path.glob('summary-*.tmp'))


@pytest.mark.parametrize('frequency_payload,expected', [({}, 200), ({'max_freq': None}, 200), ({'max_freq': 500}, 500)])
def test_reanalysis_frequency_control_preserves_recording_evidence(client, monkeypatch, frequency_payload, expected):
    ref = save_record(spectrum(), {'max_freq': 200, 'scv': 5, 'max_smoothing': .15, 'printer': 'Printer'})
    run_id = ref['run_id']
    folder = main.DATA / run_id
    (folder / 'source.stdata').write_bytes(b'original recording')
    (folder / 'history').mkdir()
    (folder / 'history' / 'older.json').write_bytes(b'prior calculation')
    original_result = (folder / 'result.json').read_bytes()
    assert client.post(f'/api/runs/{run_id}/notes', json={'text': 'Keep this note', 'measurement': 0}).status_code == 200
    assert client.put(f'/api/runs/{run_id}/view', json={'view': {'measurement': 0, 'xRange': [10, 100]}}).status_code == 200
    submitted = []
    monkeypatch.setattr(main.executor, 'submit', lambda function, ident: submitted.append((function, ident)))
    monkeypatch.setattr(main, 'updates', lambda: {})
    response = client.put(f'/api/runs/{run_id}/context', json={'axis_override': 'X', **frequency_payload})
    assert response.status_code == 200, response.text
    row = main.row_run(run_id)
    stored = json.loads(row['settings'])
    assert stored['max_freq'] == expected
    assert stored['axis_override'] == 'X'
    assert stored['scv'] == 5 and stored['max_smoothing'] == .15 and stored['printer'] == 'Printer'
    assert json.loads((folder / 'settings.json').read_text()) == stored
    assert row['status'] == 'queued' and submitted == [(main.process, run_id)]
    assert json.loads(row['view']) == {'measurement': 0, 'xRange': [10, 100]}
    assert (folder / 'source.stdata').read_bytes() == b'original recording'
    assert (folder / 'history' / 'older.json').read_bytes() == b'prior calculation'
    assert (folder / 'result.json').read_bytes() == original_result
    with main.db() as connection:
        assert connection.execute('SELECT text FROM notes WHERE run_id=?', (run_id,)).fetchone()['text'] == 'Keep this note'
    assert 'max_freq' not in main.TestContext.model_fields


@pytest.mark.parametrize('maximum', [19.99, 1000.01, 'NaN', 'Infinity', '-Infinity'])
def test_reanalysis_frequency_control_rejects_out_of_bounds_without_queueing(client, monkeypatch, maximum):
    ref = save_record(spectrum(), {'max_freq': 200})
    submitted = []
    monkeypatch.setattr(main.executor, 'submit', lambda *args: submitted.append(args))
    response = client.put(f"/api/runs/{ref['run_id']}/context", json={'max_freq': maximum})
    assert response.status_code == 422
    row = main.row_run(ref['run_id'])
    assert row['status'] == 'ready' and json.loads(row['settings'])['max_freq'] == 200
    assert not submitted
