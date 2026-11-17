import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app import main
from app.archive_import import stage_archive,group_entries,filename_info
from app.history import build_records,read_summary,write_summary


def archive_bytes(names):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
        for index,name in enumerate(names):
            payload=b'\x28\xb5\x2f\xfd'+bytes([index])*8
            member=tarfile.TarInfo(name);member.size=len(payload)
            archive.addfile(member,io.BytesIO(payload))
    return buffer.getvalue()


def test_archive_pairing_preserves_dates_and_sensor_labels(tmp_path):
    names=['inputshaper_20261114_180446_axis_Y.stdata','inputshaper_20261114_180446_axis_X.stdata',
           'inputshaper_ebb36adxl_20261115_213512_axis_X.stdata','inputshaper_ebb36adxl_20261115_213512_axis_Y.stdata']
    path=tmp_path/'source.tgz';path.write_bytes(archive_bytes(names))
    entries=stage_archive(path,tmp_path/'staged');groups,skipped=group_entries(entries)
    assert len(groups)==2 and all(len(group)==2 for group in groups)
    assert groups[0][0]['filename'].endswith('_X.stdata')
    assert filename_info(names[2])=={'recorded_at':'2026-11-15T21:35:12','source_label':'ebb36adxl'}
    assert not skipped


@pytest.mark.parametrize('bad_name',['../escape.stdata','/absolute.stdata','C:/escape.stdata','bad\\escape.stdata'])
def test_archive_traversal_rejected_without_writing_outside_staging(tmp_path,bad_name):
    path=tmp_path/'bad.tgz';path.write_bytes(archive_bytes([bad_name]))
    with pytest.raises(ValueError,match='unsafe path'):
        stage_archive(path,tmp_path/'staged')
    assert not (tmp_path/'escape.stdata').exists()


def test_archive_symlink_rejected(tmp_path):
    path=tmp_path/'bad.tgz'
    with tarfile.open(path,'w:gz') as archive:
        member=tarfile.TarInfo('linked.stdata');member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';archive.addfile(member)
    with pytest.raises(ValueError,match='links'):
        stage_archive(path,tmp_path/'staged')


def test_batch_upload_deduplicates_active_pairs_and_preserves_originals(monkeypatch):
    monkeypatch.setattr(main.executor,'submit',lambda *args:None)
    raw=archive_bytes(['inputshaper_20261114_111111_axis_X.stdata','inputshaper_20261114_111111_axis_Y.stdata'])
    with TestClient(main.app) as client:
        assert client.post('/api/batch',files={'file':('recordings.tgz',raw)}).status_code==401
        client.post('/api/login',json={'password':'test-password'})
        first=client.post('/api/batch',files={'file':('recordings.tgz',raw)})
        assert first.status_code==202,first.text
        first=first.json();assert first['created']==1 and first['reused']==0
        second=client.post('/api/batch',files={'file':('recordings.tgz',raw)}).json()
        assert second['created']==0 and second['reused']==1
        run_id=first['imports'][0]['id'];assert second['imports'][0]['id']==run_id
        manifest=json.loads((main.DATA/run_id/'sources.json').read_text())
        assert len(manifest)==2 and all(len(s['sha256'])==64 for s in manifest)
        assert all((main.DATA/run_id/s['stored']).exists() for s in manifest)
        assert client.post('/api/batch',files={'file':('bad.tgz',b'not an archive')}).status_code==400
        assert not list(main.DATA.glob('batch-*'))


def test_history_summary_cache_and_content_dedup(tmp_path,monkeypatch):
    measurement={'name':'axis_X','excitation':'X','frequency':[0,10,20],
                 'raw_psd':{'XYZ':[0,3,0],'X':[0,1,0],'Y':[0,2,0],'Z':[0,0,0]},
                 'measurement_sha256':'a'*64,'source_file':'inputshaper_20261114_111111_axis_X.stdata',
                 'quality':{'status':'pass','sample_rate_hz':3305},'diagnostic_peaks':{},
                 'signal_profile':{'gravity_plausible':True,'dominant_dc_axis':'+Z'}}
    result={'engine':{'adapter':3},'settings':{},'measurements':[measurement]}
    rows=[]
    for index in range(2):
        folder=tmp_path/str(index);folder.mkdir();(folder/'result.json').write_text(json.dumps(result))
        write_summary(folder,result);rows.append({'id':str(index),'title':'Test','filename':measurement['source_file'],'created':index})
    original=Path.read_text
    def forbid_full_result(path,*args,**kwargs):
        if path.name=='result.json':raise AssertionError('Full plot data should not be read for cached history')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',forbid_full_result)
    records,errors=build_records(tmp_path,rows)
    assert not errors and len(records)==1
    assert records[0]['duplicate_count']==2
    assert records[0]['recorded_at']=='2026-11-14T11:11:11'
    assert '3300' in records[0]['cohort_label']
