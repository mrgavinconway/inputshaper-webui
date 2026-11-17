"""Bounded .stdata archive staging and deterministic filename-based pairing."""
import hashlib
import gzip
import re
import shutil
import tarfile
import tempfile
import zlib
from datetime import datetime
from pathlib import Path, PurePosixPath

MAX_FILES = 128
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024


class _BoundedTarInfo(tarfile.TarInfo):
    """Check hidden metadata headers before stdlib reads their declared sizes."""

    def _proc_member(self, archive):
        archive._recording_header_count = getattr(archive, '_recording_header_count', 0) + 1
        if archive._recording_header_count > 1024:
            raise ValueError('Archive has too many entries, including metadata headers.')
        metadata = self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                                 tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)
        if not metadata:
            return super()._proc_member(archive)
        if not 0 <= self.size <= MAX_METADATA_BYTES:
            raise ValueError('An archive metadata header exceeds the 1 MiB limit.')
        archive._recording_metadata_depth = getattr(archive, '_recording_metadata_depth', 0) + 1
        try:
            if archive._recording_metadata_depth > 32:
                raise ValueError('Archive metadata headers are nested too deeply.')
            return super()._proc_member(archive)
        finally:
            archive._recording_metadata_depth -= 1


def filename_info(name):
    match = re.search(r'(\d{8})[_-](\d{6})', name)
    stamp = None
    if match:
        try:
            stamp = datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').isoformat()
        except ValueError:
            pass
    label = name[:match.start()].removeprefix('inputshaper_').strip('_-') if match else ''
    return {'recorded_at': stamp, 'source_label': label or 'unlabelled'}


def entry(path, filename):
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_FILE_BYTES:
        raise ValueError('Each recording must contain 1 byte–64 MiB.')
    with path.open('rb') as stream:
        if stream.read(4) != b'\x28\xb5\x2f\xfd':
            raise ValueError(f'{filename}: expected modern Zstandard .stdata, not a legacy pickle.')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': path, 'filename': filename, 'sha256': digest}


def stage_archive(path, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    try:
        # tarfile consumes PAX/GNU metadata before yielding a member. Bound the
        # entire decompressed stream first so compressed metadata cannot bypass
        # the member-count/payload limits or allocate unbounded parser memory.
        with tempfile.TemporaryFile(dir=destination) as expanded:
            expanded_size = 0
            with gzip.open(path, 'rb') as compressed:
                while chunk := compressed.read(min(1024 * 1024, MAX_BATCH_BYTES - expanded_size + 1)):
                    expanded_size += len(chunk)
                    if expanded_size > MAX_BATCH_BYTES:
                        raise ValueError('Archive expanded contents, including metadata, exceed 512 MiB.')
                    expanded.write(chunk)
            expanded.seek(0)
            entries = _stage_tar(expanded, destination)
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError) as exc:
        raise ValueError('Could not read the gzip tar archive.') from exc
    if not entries:
        raise ValueError('No .stdata recordings found in the archive.')
    return entries


def _stage_tar(expanded, destination):
    entries = []
    total = 0
    members = 0
    with tarfile.open(fileobj=expanded, mode='r:', tarinfo=_BoundedTarInfo) as archive:
        for member in archive:
            members += 1
            if members > 1024:
                raise ValueError('Archive has too many entries.')
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or '..' in pure.parts or '\\' in member.name or ':' in member.name:
                raise ValueError('Archive contains an unsafe path.')
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Archive links and special files are not accepted.')
            total += member.size
            if member.size < 0 or total > MAX_BATCH_BYTES:
                raise ValueError('Archive expanded contents exceed 512 MiB.')
            if not member.name.lower().endswith('.stdata'):
                continue
            if not 0 < member.size <= MAX_FILE_BYTES or len(entries) >= MAX_FILES:
                raise ValueError('Archive limit: 128 recordings, 64 MiB each, 512 MiB total.')
            target = destination / f'{len(entries)}.stdata'
            with archive.extractfile(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            entries.append(entry(target, pure.name[:200]))
    return entries


def group_entries(entries):
    from .diagnostics import direction
    groups = {}
    seen = set()
    skipped = []
    for item in entries:
        # Identical compressed sources cannot add an independent recording.
        signature = (item['sha256'], item['filename'])
        if signature in seen:
            skipped.append(item['filename'])
            continue
        seen.add(signature)
        name = item['filename']
        info = filename_info(name)
        axis = direction(name)
        key = re.sub(r'(?i)(^|[_-])(?:axis[_-])?[XY](?=[_.-]|$)', r'\1', name)
        if info['recorded_at'] is None or axis not in ('X', 'Y'):
            key = 'single:' + str(len(groups)) + ':' + name
        group = groups.setdefault(key, [])
        if any(direction(x['filename']) == axis for x in group):
            raise ValueError(f'Ambiguous pairing near {name}; import these recordings individually.')
        group.append(item)
    return [sorted(items, key=lambda x: direction(x['filename'])) for items in groups.values()], skipped
