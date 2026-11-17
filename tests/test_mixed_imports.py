import io
import json
import stat
import struct
import zipfile

import numpy as np
import pytest
import zstandard
from fastapi.testclient import TestClient

from app import main, archive_import as archives
from app.analysis import analyze_upload


def recording(*names, with_samples=False):
    t = np.arange(0, 4, .001)
    values = np.column_stack((t, 100*np.sin(2*np.pi*42*t), 20*np.sin(2*np.pi*70*t), np.full(len(t), 9800))).tolist() if with_samples else []
    return zstandard.ZstdCompressor().compress('\n'.join(json.dumps({'name': name, 'samples': values}) for name in names).encode())


def zip_bytes(items, compression=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=compression) as archive:
        for name, payload in items:
            archive.writestr(name, payload)
    return out.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main.executor, 'submit', lambda *args: None)
    with TestClient(main.app) as connected:
        connected.post('/api/login', json={'password': 'test-password'})
        yield connected


def mixed_items(with_samples=False, belt_axes=('A', 'B')):
    return [(f'inputshaper_20261117_120000_axis_{axis}.stdata', recording(f'axis_{axis}_20261117_120000', with_samples=with_samples)) for axis in ('X', 'Y')] + [
        ('beltscomparison_20261117_120010.stdata', recording(*(f'belt_{axis}_20261117_120010' for axis in belt_axes), with_samples=with_samples))]


@pytest.mark.parametrize('belt_axes,kinematics', [(('A', 'B'), 'corexy'), (('X', 'Z'), 'corexz')])
def test_mixed_zip_keeps_shaper_and_belts_separate_and_runs_real_engine(client, belt_axes, kinematics):
    items = mixed_items(True, belt_axes)
    response = client.post('/api/batch', files={'file': ('all.zip', zip_bytes(items))}, data={'title': 'Mixed session'})
    assert response.status_code == 202, response.text
    result = response.json()
    assert result['created'] == 2
    assert {item['test_type'] for item in result['imports']} == {'shaper', 'belts'}
    for item in result['imports']:
        folder = main.DATA/item['id']
        settings = json.loads((folder/'settings.json').read_text())
        assert settings['batch_id'] == result['batch_id']
        assert item['title'].endswith(' · Mixed session')
        analysis = analyze_upload(folder, settings)
        if item['test_type'] == 'belts':
            assert settings['kinematics'] == kinematics
            assert analysis['belt_analysis']['similarity_percent'] == pytest.approx(100)
            assert all(not m['choices'] and not m['filters'] for m in analysis['measurements'])
        else:
            assert {m['excitation'] for m in analysis['measurements']} == {'X', 'Y'}
            assert all(m['choices'] and len(m['filters']) == 5 for m in analysis['measurements'])
        manifest = json.loads((folder/'sources.json').read_text())
        assert all((folder/source['stored']).read_bytes() == dict(items)[source['filename']] for source in manifest)


def test_raw_and_zip_together_and_deduplication(client):
    items = mixed_items()
    response = client.post('/api/batch', files=[('file', ('pair.zip', zip_bytes(items[:2]))), ('file', items[2])])
    assert response.status_code == 202, response.text
    assert response.json()['created'] == 2
    again = client.post('/api/batch', files={'file': ('all.zip', zip_bytes(items))}).json()
    assert again['created'] == 0 and again['reused'] == 2


def test_split_belt_pair_and_renamed_combined_file(client):
    files = [('file', (f'beltscomparison_20261117_120000_belt_{axis}.stdata', recording(f'belt_{axis}'))) for axis in ('A', 'B')]
    result = client.post('/api/batch', files=files)
    assert result.status_code == 202 and result.json()['created'] == 1
    renamed = client.post('/api/batch', files={'file': ('renamed.stdata', recording('belt_A', 'belt_B'))})
    assert renamed.status_code == 202 and renamed.json()['imports'][0]['test_type'] == 'belts'


@pytest.mark.parametrize('belt_names,context', [(('belt_A',), {}), (('belt_A','belt_B'), {'kinematics':'corexz'}), (('axis_X','belt_B'), {})])
def test_invalid_belt_groups_create_nothing(client, belt_names, context):
    items = mixed_items()[:2] + [('beltscomparison_20261117_120010.stdata', recording(*belt_names))]
    response = client.post('/api/batch', files={'file': ('all.zip', zip_bytes(items))}, data={'context':json.dumps(context)})
    assert response.status_code == 400
    assert client.get('/api/runs').json() == []
    assert not list(main.DATA.glob('batch-*'))
    assert not [p for p in main.DATA.iterdir() if p.is_dir()]


def test_folder_and_sensor_identity_prevent_false_pairing(client):
    items=[]
    for folder in ('first', 'second'):
        for sensor, axis in [('A','X'), ('B','Y')]:
            items.append((f'{folder}/inputshaper_{sensor}_20261117_120000_axis_{axis}.stdata', recording(f'axis_{axis}_{folder}')))
    result=client.post('/api/batch',files={'file':('nested.zip',zip_bytes(items))})
    assert result.status_code==202,result.text
    assert result.json()['created']==4


@pytest.mark.parametrize('name', ['../outside.stdata', '/absolute.stdata', 'C:/absolute.stdata', 'dir\\file.stdata'])
def test_zip_unsafe_paths_rejected(tmp_path,name):
    raw=zip_bytes([(name,recording('axis_X'))])
    if '\\' in name:raw=raw.replace(name.replace('\\','/').encode(),name.encode())
    path=tmp_path/'unsafe.zip';path.write_bytes(raw)
    with pytest.raises(ValueError,match='unsafe path'): archives.stage_archive(path,tmp_path/'staged')
    assert not (tmp_path/'outside.stdata').exists()


def test_zip_links_encryption_crc_and_directory_limits(tmp_path):
    member=zipfile.ZipInfo('linked.stdata');member.create_system=3;member.external_attr=(stat.S_IFLNK|0o777)<<16
    path=tmp_path/'bad.zip';path.write_bytes(zip_bytes([(member,b'/etc/passwd')]))
    with pytest.raises(ValueError,match='links'): archives.stage_archive(path,tmp_path/'one')
    raw=bytearray(zip_bytes([('axis_X.stdata',recording('axis_X'))],zipfile.ZIP_STORED))
    central=raw.index(b'PK\x01\x02');struct.pack_into('<H',raw,central+8,1);path.write_bytes(raw)
    with pytest.raises(ValueError,match='Encrypted'): archives.stage_archive(path,tmp_path/'two')
    struct.pack_into('<H',raw,central+8,0);raw[40]^=1;path.write_bytes(raw)
    with pytest.raises(ValueError,match='damaged'): archives.stage_archive(path,tmp_path/'three')
    path.write_bytes(zip_bytes([(f'{i}.txt',b'') for i in range(1025)]))
    with pytest.raises(ValueError,match='directory exceeds'): archives.stage_archive(path,tmp_path/'four')


def test_cumulative_archives_cannot_bypass_recording_limit(client,monkeypatch):
    monkeypatch.setattr(archives,'MAX_FILES',3)
    items=mixed_items()[:2]
    response=client.post('/api/batch',files=[('file',(f'{i}.zip',zip_bytes(items))) for i in range(2)])
    assert response.status_code==400 and 'expanded limit' in response.text
    assert client.get('/api/runs').json()==[]


def test_zip_ignored_entries_still_count_expanded_bytes(tmp_path,monkeypatch):
    monkeypatch.setattr(archives,'MAX_BATCH_BYTES',1000)
    path=tmp_path/'large.zip';path.write_bytes(zip_bytes([('large.txt',b'0'*1001),('x.stdata',recording('axis_X'))]))
    with pytest.raises(ValueError,match='expanded limit'): archives.stage_archive(path,tmp_path/'staged')
