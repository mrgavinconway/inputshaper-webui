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
                                str(folder / 'settings.json'), str(folder / 'result.json')],
                               cwd=ROOT, stdout=log, stderr=log, timeout=600)
        if p.returncode:
            tail = (folder / 'processing.log').read_text(errors='replace').strip().splitlines()[-1:]
            raise ValueError('Analysis failed: ' + ''.join(tail)[-700:])
        with db() as c:
            c.execute("UPDATE runs SET status='ready', error=NULL WHERE id=?", (run_id,))
    except Exception as e:
        with db() as c:
            c.execute("UPDATE runs SET status='failed', error=? WHERE id=?", (str(e), run_id))

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
        if path == '/api/runs':
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

@app.post('/api/runs', status_code=202)
async def upload(file: list[UploadFile] = File(...), title: str = Form(''), printer: str = Form(''),
                 conditions: str = Form(''), scv: float = Form(5), max_freq: float = Form(200),
                 max_smoothing: str = Form('')):
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
            sources.append({'stored': stored, 'filename': original})
        (folder / 'sources.json').write_text(json.dumps(sources))
        filenames = ' + '.join(s['filename'] for s in sources)
        settings = {'scv': scv, 'max_freq': max_freq, 'max_smoothing': smoothing,
                    'printer': printer[:200], 'conditions': conditions[:4000]}
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
    measurement: int = Field(default=0, ge=0, le=11)
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
