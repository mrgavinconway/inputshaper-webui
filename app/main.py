import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from typing import Literal
from .diagnostics import compare, compatibility, band_power
from .history import read_summary, write_summary, recording_keys, build_records, band_metrics, cohort
from .result_summary import source_digest

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('INPUTSHAPER_DATA', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
PASSWORD = os.environ.get('INPUTSHAPER_PASSWORD', '')
SESSION = hmac.new(PASSWORD.encode(), b'inputshaper-session-v1', 'sha256').hexdigest()
executor = ThreadPoolExecutor(max_workers=1)
jobs_lock = threading.Lock()

def db():
    c = sqlite3.connect(DATA / 'library.db', timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, title TEXT, filename TEXT, created REAL, status TEXT, error TEXT, settings TEXT, view TEXT DEFAULT \'{}\')')
        c.execute('CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, run_id TEXT, created REAL, text TEXT, measurement INTEGER, low REAL, high REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY, created REAL, updated REAL, body TEXT)')
        if 'deleted' not in {r['name'] for r in c.execute('PRAGMA table_info(runs)')}:
            c.execute('ALTER TABLE runs ADD COLUMN deleted REAL')
        c.execute("UPDATE runs SET status='failed', error='Processing interrupted by service restart. Re-upload the original file.' WHERE status IN ('queued','processing')")

def row_run(run_id):
    with db() as c:
        row = c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Run not found')
    return dict(row)

def process(run_id):
    folder = DATA / run_id
    with db() as c:
        c.execute("UPDATE runs SET status='processing' WHERE id=?", (run_id,))
    try:
        with (folder / 'processing.log').open('w') as log:
            p = subprocess.run([sys.executable, '-m', 'app.analysis', str(folder),
                                str(folder / 'settings.json'), str(folder / 'result.pending.json')],
                               cwd=ROOT, stdout=log, stderr=log, timeout=600)
        if p.returncode:
            tail = (folder / 'processing.log').read_text(errors='replace').strip().splitlines()[-1:]
            raise ValueError('Analysis failed: ' + ''.join(tail)[-700:])
        if (folder / 'result.json').exists():
            import shutil
            history=folder/'history';history.mkdir(exist_ok=True)
            shutil.copy2(folder/'result.json', history/f'{time.time_ns()}.json')
        (folder/'result.pending.json').replace(folder/'result.json')
        try:
            write_summary(folder, json.loads((folder/'result.json').read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError):
            # The derived history index can be rebuilt on demand. A failed
            # cache write must not turn a successful analysis into a failure.
            pass
        with db() as c:
            c.execute("UPDATE runs SET status='ready', error=NULL WHERE id=?", (run_id,))
    except Exception as e:
        previous_settings=None
        if (folder/'result.json').exists():
            previous_settings=json.loads((folder/'result.json').read_text())['settings']
            (folder/'settings.json').write_text(json.dumps(previous_settings))
        with db() as c:
            c.execute("UPDATE runs SET status=?, error=? WHERE id=?", ('ready' if (folder/'result.json').exists() else 'failed',str(e), run_id))
            if previous_settings is not None:c.execute('UPDATE runs SET settings=? WHERE id=?',(json.dumps(previous_settings),run_id))

@asynccontextmanager
async def lifespan(app):
    if not PASSWORD:
        raise RuntimeError('Set INPUTSHAPER_PASSWORD before starting the server.')
    init_db()
    yield

app = FastAPI(title='Input Shaper Lab', lifespan=lifespan)

@app.middleware('http')
async def access(request: Request, call_next):
    path = request.url.path
    if path.startswith('/api/') and path not in ('/api/login', '/api/health'):
        if not hmac.compare_digest(request.cookies.get('session', ''), SESSION):
            return JSONResponse({'detail': 'Sign in to continue'}, 401)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin request rejected'}, 403)
        if path in ('/api/runs', '/api/batch'):
            try:
                update_state = json.loads(Path('/var/lib/inputshaper-update/status.json').read_text())
                if update_state.get('status') == 'installing':
                    return JSONResponse({'detail': 'An update is being installed. Please wait before importing.'}, 409)
            except (OSError, ValueError):
                pass
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    if path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

login_attempts = {}

class Login(BaseModel):
    password: str = Field(max_length=256)

@app.post('/api/login')
def login(body: Login, request: Request):
    key = request.client.host
    now = time.time()
    attempts = [t for t in login_attempts.get(key, []) if now - t < 60]
    if len(attempts) >= 10:
        raise HTTPException(429, 'Too many attempts; try again in a minute.')
    if not hmac.compare_digest(body.password, PASSWORD):
        login_attempts[key] = attempts + [now]
        raise HTTPException(401, 'Incorrect password')
    login_attempts.pop(key, None)
    response = JSONResponse({'ok': True})
    response.set_cookie('session', SESSION, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https', max_age=86400 * 7)
    return response

@app.post('/api/logout')
def logout():
    r = JSONResponse({'ok': True})
    r.delete_cookie('session')
    return r

@app.get('/api/health')
def health():
    with db() as c:
        c.execute('SELECT 1')
    return {'status': 'ok', 'version': version()}

@app.get('/api/runs')
def runs(deleted: bool = False):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id,title,filename,created,status,error,deleted FROM runs WHERE deleted IS '+('NOT NULL' if deleted else 'NULL')+' ORDER BY created DESC')]

@app.get('/api/catalog')
def catalog():
    out=[]
    with db() as c:
        rows=c.execute("SELECT id,title FROM runs WHERE status='ready' AND deleted IS NULL").fetchall()
    for r in rows:
        result=read_summary(DATA/r['id'])
        out.extend({'run_id':r['id'],'title':r['title'],'measurement':i,'name':m['name'],
                    'excitation':m.get('excitation','unknown'),'test_type':result.get('test_type','shaper')}
                   for i,m in enumerate(result['measurements']))
    return out

def automatic_title(sources):
    for source in sources:
        match = re.search(r'(\d{8})[_-](\d{6})', source['filename'])
        if match:
            try:
                stamp = datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S')
                return stamp.strftime('%d %b %Y, %H:%M:%S') + (' · X/Y pair' if len(sources) == 2 else '')
            except ValueError:
                pass
    # No trustworthy file timestamp: retain descriptive filenames rather than inventing a test date.
    return ' + '.join(Path(s['filename']).stem for s in sources)[:200]

class TestContext(BaseModel):
    test_type: Literal['shaper','belts','static','vibrations'] = 'shaper'
    axis_override: Literal['auto','X','Y','Z','A','B','unknown'] = 'auto'
    test_mode: str = Field(default='unknown',max_length=120)
    sensor: str = Field(default='',max_length=120)
    sensor_mount: str = Field(default='',max_length=200)
    axes_map: str = Field(default='',max_length=120)
    position: str = Field(default='',max_length=120)
    accel_per_hz: str = Field(default='',max_length=50)
    sweep_rate: str = Field(default='',max_length=50)
    fans: str = Field(default='',max_length=200)
    temperature: str = Field(default='',max_length=120)
    kinematics: Literal['unknown','corexy','corexz','cartesian'] = 'unknown'
    static_frequency: float | None = Field(default=None,gt=0,le=1000)
    acquisition_group: str = Field(default='',max_length=200)
    umbilical: str = Field(default='',max_length=500)
    intervention: str = Field(default='',max_length=1000)
    measurement_axes: list[Literal['auto','X','Y','Z','A','B','unknown']] = Field(default_factory=list,max_length=512)

@app.put('/api/runs/{run_id}/context')
def context_update(run_id: str, body: TestContext):
    r=row_run(run_id)
    if r['status'] in ('processing','queued'):raise HTTPException(409,'Analysis is already running.')
    if updates().get('status')=='installing':raise HTTPException(409,'Wait for the service update to finish.')
    settings={**json.loads(r['settings']),**body.model_dump()}
    (DATA/run_id/'settings.json').write_text(json.dumps(settings))
    with db() as c:
        c.execute("UPDATE runs SET settings=?,status='queued',error=NULL WHERE id=?",(json.dumps(settings),run_id))
    executor.submit(process,run_id)
    return {'ok':True}

class MeasurementRef(BaseModel):
    run_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    measurement: int = Field(default=0,ge=0,le=511)

def resolve_ref(ref):
    r=row_run(ref.run_id)
    if r['status']!='ready':raise HTTPException(409,'All selected analyses must be ready.')
    result=read_summary(DATA/ref.run_id)
    try:m=result['measurements'][ref.measurement]
    except IndexError:raise HTTPException(400,'Measurement not found')
    return m,result['settings'],result['engine']

class CompareRequest(BaseModel):
    current: MeasurementRef
    baseline: MeasurementRef
    low: float = Field(ge=0,le=1000)
    high: float = Field(gt=0,le=1000)
    repeats: list[MeasurementRef] = Field(default_factory=list,max_length=10)

@app.post('/api/compare')
def compare_measurements(body: CompareRequest):
    a,sa,ea=resolve_ref(body.current);b,sb,eb=resolve_ref(body.baseline)
    try:result=compare(a,b,sa,sb,body.low,body.high,ea,eb)
    except ValueError as e:raise HTTPException(400,str(e))
    result['repeatability']={'count':0,'axes':{},'excluded':[]}
    if not result['compatibility']['comparable']:return result
    seen={(body.current.run_id,body.current.measurement)};seen_recordings=recording_keys(a);accepted=[]
    for ref in [body.baseline,*body.repeats]:
        key=(ref.run_id,ref.measurement)
        if key in seen:continue
        seen.add(key);m,s,e=resolve_ref(ref)
        identities=recording_keys(m)
        if identities.intersection(seen_recordings):
            result['repeatability']['excluded'].append({'ref':ref.model_dump(),'reason':['Duplicate recording content; not an independent repeat.']});continue
        seen_recordings.update(identities)
        check=compatibility(m,b,s,sb,e,eb)
        # Repeats must match known experimental conditions, not just acquisition settings.
        differences=[k for k in ('fans','temperature','position','conditions','acquisition_group','umbilical','intervention') if s.get(k)!=sb.get(k)]
        if not check['comparable'] or differences:
            result['repeatability']['excluded'].append({'ref':ref.model_dump(),'reason':check['blockers']+differences});continue
        try:accepted.append({axis:band_power(m,body.low,body.high,axis) for axis in ('XYZ','X','Y','Z')})
        except ValueError as e:result['repeatability']['excluded'].append({'ref':ref.model_dump(),'reason':[str(e)]})
    result['repeatability']['count']=len(accepted)
    if len(accepted)>=2:
        for axis in ('XYZ','X','Y','Z'):
            values=[x[axis] for x in accepted];mean=sum(values)/len(values)
            result['repeatability']['axes'][axis]={'mean':mean,'min':min(values),'max':max(values),
                'current_outside_observed_range':not min(values)<=result['axes'][axis]['after_power']<=max(values)}
    result['repeatability']['qualification']='Observed baseline range only; not statistical significance. Exact duplicate recordings are excluded where fingerprints exist. Different files still require unchanged conditions; missing context remains unverified.'
    return result

class Investigation(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    hypothesis: str = Field(default='',max_length=4000)
    expected: str = Field(default='',max_length=4000)
    test: str = Field(default='',max_length=4000)
    observation: str = Field(default='',max_length=4000)
    conclusion: str = Field(default='',max_length=4000)
    status: Literal['open','supported','not_supported','inconclusive']='open'
    references: list[MeasurementRef] = Field(default_factory=list,max_length=30)
    band: list[float] = Field(default_factory=list,max_length=2)

@app.get('/api/investigations')
def investigations():
    with db() as c:return [{'id':r['id'],**json.loads(r['body'])} for r in c.execute('SELECT * FROM investigations ORDER BY updated DESC')]

@app.put('/api/investigations/{investigation_id}')
def save_investigation(investigation_id: str,body: Investigation):
    if not re.fullmatch(r'[a-f0-9-]{32,36}',investigation_id):raise HTTPException(400,'Invalid investigation identifier')
    if body.band and (len(body.band)!=2 or not 0<=body.band[0]<body.band[1]<=1000):raise HTTPException(400,'Invalid frequency band')
    payload=body.model_dump();payload['evidence_saved_at']=time.time();payload['evidence']=[]
    for ref in body.references:
        m,s,e=resolve_ref(ref)
        snapshot={'reference':ref.model_dump(),'name':m['name'],'settings':s,'engine':e,
                  'quality':m.get('quality'),'excitation':m.get('excitation'),'peaks':m.get('diagnostic_peaks')}
        if body.band and m.get('raw_psd'):
            try:snapshot['band_power']={axis:band_power(m,*body.band,axis) for axis in ('XYZ','X','Y','Z')}
            except ValueError:snapshot['band_power_status']='Outside this recording’s frequency range'
        payload['evidence'].append(snapshot)
    with db() as c:c.execute('INSERT INTO investigations VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,body=excluded.body',
                            (investigation_id,time.time(),time.time(),json.dumps(payload)))
    return {'ok':True}

@app.post('/api/runs', status_code=202)
async def upload(file: list[UploadFile] = File(...), title: str = Form(''), printer: str = Form(''),
                 conditions: str = Form(''), scv: float = Form(5), max_freq: float = Form(200),
                 max_smoothing: str = Form(''), context: str = Form('{}')):
    try:
        details=TestContext.model_validate_json(context).model_dump()
    except ValueError as e:
        raise HTTPException(400, 'Invalid test context: '+str(e)[:300])
    if not 1 <= len(file) <= 2:
        raise HTTPException(400, 'Select one .stdata file or an X/Y pair (maximum two files).')
    if any(not (f.filename or '').lower().endswith('.stdata') for f in file):
        raise HTTPException(400, 'Every selected file must be a .stdata file')
    if not 0 <= scv <= 100 or not 20 <= max_freq <= 1000:
        raise HTTPException(400, 'SCV must be 0–100 and maximum frequency 20–1000 Hz')
    try:
        smoothing = float(max_smoothing) if max_smoothing else None
        if smoothing is not None and not 0.001 <= smoothing <= 10:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Smoothing must be blank or between 0.001 and 10')
    run_id = uuid.uuid4().hex
    folder = DATA / run_id
    folder.mkdir()
    total = 0
    try:
        sources = []
        for i, uploaded in enumerate(file):
            stored = 'source.stdata' if len(file) == 1 else f'source-{i}.stdata'
            size = 0
            with (folder / stored).open('wb') as out:
                while chunk := await uploaded.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise HTTPException(413, 'Each file must be at most 64 MiB')
                    out.write(chunk)
            if not size:
                raise HTTPException(400, 'One of the selected files is empty')
            original = uploaded.filename.replace('\\', '/').rsplit('/', 1)[-1][:200]
            sources.append({'stored': stored, 'filename': original, 'sha256': source_digest(folder / stored)})
        (folder / 'sources.json').write_text(json.dumps(sources))
        filenames = ' + '.join(s['filename'] for s in sources)
        settings = {'scv': scv, 'max_freq': max_freq, 'max_smoothing': smoothing,
                    'printer': printer[:200], 'conditions': conditions[:4000], **details}
        (folder / 'settings.json').write_text(json.dumps(settings))
        with db() as c:
            c.execute('INSERT INTO runs(id,title,filename,created,status,settings) VALUES(?,?,?,?,?,?)',
                      (run_id, (title.strip() or automatic_title(sources))[:200], filenames, time.time(), 'queued', json.dumps(settings)))
        executor.submit(process, run_id)
        return {'id': run_id}
    except Exception:
        import shutil
        shutil.rmtree(folder)
        raise

@app.get('/api/runs/{run_id}')
def run(run_id: str):
    r = row_run(run_id)
    r['settings'] = json.loads(r['settings'])
    r['view'] = json.loads(r['view'])
    with db() as c:
        r['notes'] = [dict(n) for n in c.execute('SELECT * FROM notes WHERE run_id=? ORDER BY created', (run_id,))]
    if r['status'] == 'ready':
        r['result'] = json.loads((DATA / run_id / 'result.json').read_text())
    return r

@app.delete('/api/runs/{run_id}')
def delete_run(run_id: str):
    r = row_run(run_id)
    if r['status'] in ('queued', 'processing'):
        raise HTTPException(409, 'Wait for analysis to finish before deleting this import.')
    with db() as c:
        c.execute('UPDATE runs SET deleted=? WHERE id=?', (time.time(), run_id))
    return {'ok': True}

@app.post('/api/runs/{run_id}/restore')
def restore_run(run_id: str):
    row_run(run_id)
    with db() as c:
        c.execute('UPDATE runs SET deleted=NULL WHERE id=?', (run_id,))
    return {'ok': True}

class View(BaseModel):
    view: dict

@app.put('/api/runs/{run_id}/view')
def save_view(run_id: str, body: View):
    row_run(run_id)
    value = json.dumps(body.view)
    if len(value) > 30000:
        raise HTTPException(400, 'View is too large')
    with db() as c:
        c.execute('UPDATE runs SET view=? WHERE id=?', (value, run_id))
    return {'ok': True}

class Note(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    measurement: int = Field(default=0, ge=0, le=511)
    low: float | None = Field(default=None, ge=0, le=1000)
    high: float | None = Field(default=None, ge=0, le=1000)

@app.post('/api/runs/{run_id}/notes')
def add_note(run_id: str, note: Note):
    row_run(run_id)
    if note.high is not None and note.low is not None and note.high < note.low:
        raise HTTPException(400, 'Band end must be greater than start')
    with db() as c:
        c.execute('INSERT INTO notes VALUES(?,?,?,?,?,?,?)',
                  (uuid.uuid4().hex, run_id, time.time(), note.text, note.measurement, note.low, note.high))
    return {'ok': True}

@app.get('/api/runs/{run_id}/export')
def export(run_id: str):
    return JSONResponse(run(run_id), headers={'Content-Disposition': f'attachment; filename="analysis-{run_id[:8]}.json"'})

@app.get('/api/runs/{run_id}/source')
def source(run_id: str):
    row_run(run_id)
    folder = DATA / run_id
    manifest = folder / 'sources.json'
    sources = json.loads(manifest.read_text()) if manifest.exists() else [{'stored':'source.stdata', 'filename':f'{run_id}.stdata'}]
    if len(sources) == 1:
        return FileResponse(folder / sources[0]['stored'], filename=sources[0]['filename'])
    import io
    import zipfile
    from fastapi.responses import Response
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        for i, entry in enumerate(sources):
            archive.write(folder / entry['stored'], arcname=f'{i+1}-{entry["filename"]}')
    return Response(data.getvalue(), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="originals-{run_id[:8]}.zip"'})

def version():
    try:
        return subprocess.check_output(['git', '-c', 'safe.directory='+str(ROOT), 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=5).strip()
    except Exception:
        return 'development'

@app.get('/api/updates')
def updates():
    state_file = Path(os.environ.get('INPUTSHAPER_UPDATE_STATE', '/var/lib/inputshaper-update/status.json'))
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {'status': 'unavailable', 'message': 'Updates are available after Linux service installation.'}
    return {'current': version(), **state}

@app.post('/api/updates/{action}')
def update_action(action: str):
    units = {'check': 'inputshaper-check.service', 'install': 'inputshaper-update.service'}
    if action not in units or sys.platform != 'linux':
        raise HTTPException(400, 'Update action unavailable')
    if action == 'install':
        with db() as c:
            if c.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','processing')").fetchone()[0]:
                raise HTTPException(409, 'Wait for current analyses to finish before updating.')
        state = updates()
        if not state.get('available'):
            raise HTTPException(409, 'Check for a new version first.')
    p = subprocess.run(['sudo', '-n', '/usr/bin/systemctl', 'start', '--no-block', units[action]], capture_output=True, text=True)
    if p.returncode:
        raise HTTPException(503, 'Update service could not be started. Check the service configuration.')
    return {'ok': True}

app.mount('/static', StaticFiles(directory=ROOT / 'app/static'), name='static')

@app.get('/')
def index():
    return FileResponse(ROOT / 'app/static/index.html')

@app.get('/api/history')
def recording_history():
    with db() as connection:
        rows = [dict(r) for r in connection.execute("SELECT * FROM runs WHERE deleted IS NULL ORDER BY created DESC")]
    records, errors = build_records(DATA, [r for r in rows if r['status'] == 'ready'])
    return {'records': records, 'errors': errors,
            'counts': {status: sum(r['status'] == status for r in rows) for status in ('ready','queued','processing','failed')},
            'qualification': 'Groups are inferred from filename labels, sample rate and apparent gravity direction. Matching groups do not verify unchanged mounting, excitation, temperature or mechanical conditions. Filename times have no recorded timezone.'}


class HistoryBand(BaseModel):
    references: list[MeasurementRef] = Field(min_length=1, max_length=128)
    low: float = Field(ge=0, le=1000)
    high: float = Field(gt=0, le=1000)
    axis: Literal['XYZ','X','Y','Z'] = 'XYZ'


@app.post('/api/history/band')
def history_band(body: HistoryBand):
    if body.high <= body.low:
        raise HTTPException(400, 'Band end must be greater than its start.')
    output = []
    for ref in body.references:
        try:
            measurement, settings, engine = resolve_ref(ref)
            record = {**measurement, 'run_id': ref.run_id, 'measurement': ref.measurement, 'settings': settings}
            from .archive_import import filename_info
            record.update(filename_info(record.get('source_file', record['name'])))
            record['cohort_key'], _ = cohort(record)
            output.append(band_metrics(record, body.low, body.high, body.axis))
        except (ValueError, HTTPException) as exc:
            output.append({**ref.model_dump(), 'error': str(exc.detail) if isinstance(exc,HTTPException) else str(exc)})
    return {'records': output, 'band': [body.low,body.high], 'axis': body.axis,
            'warnings': ['Band RMS is measured acceleration at the sensor, not nozzle-to-bed motion or a machine-health score.']}


class ChangesRequest(BaseModel):
    baseline: MeasurementRef
    current: MeasurementRef
    low: float = Field(default=10, ge=0, le=1000)
    high: float = Field(default=200, gt=0, le=1000)


@app.post('/api/changes')
def recording_changes(body: ChangesRequest):
    from .changes import summarize_changes
    a, sa, ea = resolve_ref(body.current)
    b, sb, eb = resolve_ref(body.baseline)
    try:
        result = summarize_changes(a, b, sa, sb, ea, eb, body.low, body.high)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    result['references'] = {'baseline': body.baseline.model_dump(), 'current': body.current.model_dump()}
    result['titles'] = {'baseline': row_run(body.baseline.run_id)['title'], 'current': row_run(body.current.run_id)['title']}
    return result


def create_batch_imports(entries, settings):
    import shutil
    from .archive_import import group_entries, filename_info
    groups, skipped = group_entries(entries)
    imports = []
    created = []
    batch_id = uuid.uuid4().hex
    with jobs_lock:
        try:
            with db() as connection:
                existing = {}
                rows = connection.execute("SELECT id,title FROM runs WHERE deleted IS NULL AND status != 'failed'").fetchall()
                for row in rows:
                    folder = DATA / row['id']
                    manifest = folder / 'sources.json'
                    try:
                        sources = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else [{'stored':'source.stdata'}]
                        hashes = tuple(sorted(s.get('sha256') or source_digest(folder/s['stored']) for s in sources))
                    except (OSError, ValueError, KeyError):
                        continue  # Retained results without originals cannot establish a source match.
                    existing[hashes] = dict(row)
                for group in groups:
                    signature = tuple(sorted(item['sha256'] for item in group))
                    if signature in existing:
                        imports.append({**existing[signature], 'reused': True})
                        continue
                    run_id = uuid.uuid4().hex
                    folder = DATA / run_id
                    folder.mkdir()
                    created.append(run_id)
                    sources = []
                    for index,item in enumerate(group):
                        stored = f'source-{index}.stdata'
                        shutil.copyfile(item['path'], folder/stored)
                        sources.append({'stored':stored, 'filename':item['filename'], 'sha256':item['sha256']})
                    info = filename_info(sources[0]['filename'])
                    run_settings = {**settings, 'batch_id':batch_id, 'source_label':info['source_label']}
                    title = automatic_title(sources)
                    if info['source_label'] != 'unlabelled':
                        title += ' · ' + info['source_label']
                    (folder/'sources.json').write_text(json.dumps(sources),encoding='utf-8')
                    (folder/'settings.json').write_text(json.dumps(run_settings),encoding='utf-8')
                    connection.execute('INSERT INTO runs(id,title,filename,created,status,settings) VALUES(?,?,?,?,?,?)',
                        (run_id,title[:200],' + '.join(s['filename'] for s in sources),time.time(),'queued',json.dumps(run_settings)))
                    item = {'id':run_id,'title':title[:200]}
                    existing[signature] = item
                    imports.append({**item,'reused':False})
        except Exception:
            for run_id in created:
                shutil.rmtree(DATA/run_id)
            raise
    for run_id in created:
        executor.submit(process,run_id)
    return {'batch_id':batch_id,'imports':imports,'skipped':skipped,'created':len(created),
            'reused':sum(item['reused'] for item in imports)}


@app.post('/api/batch', status_code=202)
async def batch_upload(file: list[UploadFile] = File(...), printer: str = Form(''), conditions: str = Form(''),
                       scv: float = Form(5), max_freq: float = Form(500), max_smoothing: str = Form(''), context: str = Form('{}')):
    import tempfile
    from starlette.concurrency import run_in_threadpool
    from .archive_import import MAX_FILES, MAX_FILE_BYTES, MAX_BATCH_BYTES, stage_archive, entry
    try:
        details = TestContext.model_validate_json(context).model_dump()
        smoothing = float(max_smoothing) if max_smoothing else None
        if not 0 <= scv <= 100 or not 20 <= max_freq <= 1000 or (smoothing is not None and not .001 <= smoothing <= 10):
            raise ValueError('Invalid calibration settings.')
        if not 1 <= len(file) <= MAX_FILES:
            raise ValueError('Choose one .tgz archive or up to 128 .stdata recordings.')
        names = [(f.filename or '').replace('\\','/').rsplit('/',1)[-1][:200] for f in file]
        archive = len(file) == 1 and names[0].lower().endswith(('.tgz','.tar.gz'))
        if not archive and any(not name.lower().endswith('.stdata') for name in names):
            raise ValueError('Choose one .tgz archive or a selection of .stdata files.')
        settings = {'scv':scv,'max_freq':max_freq,'max_smoothing':smoothing,'printer':printer[:200],
                    'conditions':conditions[:4000],**details}
        with tempfile.TemporaryDirectory(prefix='batch-',dir=DATA) as staging:
            staging = Path(staging)
            entries = []
            total = 0
            for index,uploaded in enumerate(file):
                target = staging/f'upload-{index}'
                size = 0
                with target.open('wb') as output:
                    while chunk := await uploaded.read(1024*1024):
                        size += len(chunk); total += len(chunk)
                        if total > MAX_BATCH_BYTES or size > (MAX_BATCH_BYTES if archive else MAX_FILE_BYTES):
                            raise HTTPException(413,'Batch limit: 512 MiB total, 64 MiB per recording.')
                        output.write(chunk)
                if archive:
                    entries = await run_in_threadpool(stage_archive,target,staging/'recordings')
                else:
                    entries.append(await run_in_threadpool(entry,target,names[index]))
            return await run_in_threadpool(create_batch_imports,entries,settings)
    except ValueError as exc:
        raise HTTPException(400,str(exc)[:500])
