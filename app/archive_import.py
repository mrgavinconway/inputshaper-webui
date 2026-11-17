"""Bounded .stdata archive staging and deterministic filename-based pairing."""
import hashlib
import gzip
import io
import json
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import zipfile
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


def charge(budget, size=0, files=0):
    budget['bytes'] += size
    budget['files'] += files
    if budget['bytes'] > MAX_BATCH_BYTES or budget['files'] > MAX_FILES:
        raise ValueError('Batch expanded limit: 128 recordings and 512 MiB total, including archive metadata.')


def safe_path(name):
    pure = PurePosixPath(name)
    if pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name or '\0' in name:
        raise ValueError('Archive contains an unsafe path.')
    return pure


def stage_archive(path, destination, budget=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    budget = budget if budget is not None else {'bytes': 0, 'files': 0}
    entries = []
    try:
        with open(path, 'rb') as source:
            is_zip = source.read(2) == b'PK'
        if is_zip:
            entries = _stage_zip(path, destination, budget)
            if not entries:
                raise ValueError('No .stdata recordings found in the archive.')
            return entries
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
                    charge(budget, len(chunk))
                    expanded.write(chunk)
            expanded.seek(0)
            entries = _stage_tar(expanded, destination, budget)
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError) as exc:
        raise ValueError('Could not read the gzip tar archive.') from exc
    if not entries:
        raise ValueError('No .stdata recordings found in the archive.')
    return entries


def _stage_tar(expanded, destination, budget):
    entries = []
    total = 0
    members = 0
    with tarfile.open(fileobj=expanded, mode='r:', tarinfo=_BoundedTarInfo) as archive:
        for member in archive:
            members += 1
            if members > 1024:
                raise ValueError('Archive has too many entries.')
            pure = safe_path(member.name)
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
            charge(budget, files=1)
            target = destination / f'{len(entries)}.stdata'
            with archive.extractfile(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            entries.append({**entry(target, pure.name[:200]), 'group_path': str(pure.parent)})
    return entries


def _stage_zip(path, destination, budget):
    # Bound the central directory before ZipFile allocates and parses it.
    with open(path, 'rb') as source:
        source.seek(0, 2)
        length = source.tell()
        source.seek(max(0, length - 65557))
        tail = source.read(65557)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError('Could not read the ZIP archive directory.')
    _, disk, directory_disk, on_disk, count, size, start, comment = struct.unpack_from('<4s4H2LH', tail, offset)
    if disk or directory_disk or on_disk != count:
        raise ValueError('Split ZIP archives are not supported.')
    if count == 65535 or size == 0xffffffff or start == 0xffffffff:
        raise ValueError('ZIP64 directories are not needed within the upload limit; save a standard ZIP archive.')
    if count > 1024 or size > MAX_METADATA_BYTES or offset + 22 + comment != len(tail) or start + size > length:
        raise ValueError('ZIP directory exceeds the entry/metadata limit or is invalid.')
    charge(budget, size + 22 + comment)
    entries = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != count:
                raise ValueError('ZIP directory entry count is inconsistent.')
            for member in members:
                pure = safe_path(member.orig_filename)
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError('Archive links and special files are not accepted.')
                if member.flag_bits & 1:
                    raise ValueError('Encrypted ZIP archives are not supported.')
                charge(budget, member.file_size)
                if member.is_dir():
                    continue
                if mode == stat.S_IFDIR:
                    raise ValueError('ZIP directory metadata is inconsistent.')
                if not member.filename.lower().endswith('.stdata'):
                    continue
                if not 0 < member.file_size <= MAX_FILE_BYTES:
                    raise ValueError('Each recording must contain 1 byte–64 MiB.')
                charge(budget, files=1)
                target = destination / f'{len(entries)}.stdata'
                written = 0
                with archive.open(member) as source, target.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > member.file_size or written > MAX_FILE_BYTES:
                            raise ValueError('ZIP entry exceeds its declared recording size.')
                        output.write(chunk)
                if written != member.file_size:
                    raise ValueError('ZIP entry is incomplete.')
                entries.append({**entry(target, pure.name[:200]), 'group_path': str(pure.parent)})
    except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise ValueError('Could not read the ZIP archive; it may be damaged.') from exc
    except (NotImplementedError, RuntimeError) as exc:
        raise ValueError('Unsupported ZIP compression or encryption; use a standard unencrypted ZIP.') from exc
    return entries


def recording_names(path):
    """Read bounded JSON records one at a time; retain only names for routing."""
    import zstandard
    names = []
    maximum = 256 * 1024 * 1024
    total = 0
    try:
        with open(path, 'rb') as source, zstandard.ZstdDecompressor().stream_reader(source) as compressed, io.BufferedReader(compressed) as stream:
            while line := stream.readline(maximum - total + 1):
                total += len(line)
                if total > maximum:
                    raise ValueError('Decompressed recording exceeds 256 MiB.')
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError('Expected a JSON measurement object.')
                names.append(str(record.get('name', 'Measurement'))[:200])
                del record
                if len(names) > 512:
                    raise ValueError('Maximum 512 measurements per recording.')
    except (zstandard.ZstdError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Could not read modern .stdata measurement names.') from exc
    if not names:
        raise ValueError('No measurements found in the recording.')
    return names


def test_hint(name):
    name = name.lower()
    for kind, pattern in (
        ('unsupported', r'(?:^|[_\W])axesmap(?:[_\W]|$)'),
        ('belts', r'(?:^|[_\W])(?:beltscomparison|belts|belt)(?:[_\W]|$)'),
        ('static', r'(?:^|[_\W])(?:staticfreq|staticfrequency)(?:[_\W]|$)'),
        ('vibrations', r'(?:^|[_\W])(?:vibrations|vib)(?:[_\W]|$)'),
        ('shaper', r'(?:^|[_\W])(?:inputshaper|axis)(?:[_\W]|$)'),
    ):
        if re.search(pattern, name):
            return kind
    return None


def classify_entries(entries, settings, auto_detect=True):
    from .diagnostics import direction
    result = []
    for item in entries:
        names = recording_names(item['path'])
        hints = {hint for text in [item['filename'], *names] if (hint := test_hint(text))}
        if 'unsupported' in hints:
            raise ValueError(f"{item['filename']}: axes-map calibration is not supported.")
        if auto_detect and len(hints) > 1:
            raise ValueError(f"{item['filename']}: conflicting test labels; separate different test types.")
        axes = [direction(name) if direction(name) != 'unknown' else direction(item['filename']) for name in names]
        kind = (next(iter(hints)) if hints else ('belts' if set(axes) <= {'A', 'B'} else settings.get('test_type', 'shaper'))) if auto_detect else settings.get('test_type', 'shaper')
        result.append({**item, 'test_type': kind, 'measurement_names': names, 'directions': axes})
    return result


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
        kind = item.get('test_type', 'shaper')
        axes = item.get('directions', [direction(name)])
        axis = axes[0] if len(axes) == 1 else 'unknown'
        pair_axes = ('A', 'B', 'X', 'Z') if kind == 'belts' else ('X', 'Y')
        explicit = r'(?i)(^|[_-])(?:axis|belt)[_-][XYZAB](?=[_.-]|$)'
        # Remove only the direction token: a sensor label such as A stays part
        # of the session identity, so two different sensors never form a pair.
        key = re.sub(explicit, r'\1direction', name, count=1) if re.search(explicit, name) else re.sub(
            r'(?i)(^|[_-])' + re.escape(axis) + r'(?=[_.-]|$)', r'\1direction', name, count=1)
        if info['recorded_at'] is None or axis not in pair_axes or len(axes) != 1 or kind not in ('shaper', 'belts'):
            key = 'single:' + str(len(groups)) + ':' + name
        key = (item.get('namespace', ''), item.get('group_path', ''), kind, key)
        group = groups.setdefault(key, [])
        if any(x.get('directions', [direction(x['filename'])]) == axes for x in group):
            raise ValueError(f'Ambiguous pairing near {name}; import these recordings individually.')
        group.append(item)
    return [sorted(items, key=lambda x: x.get('directions', [direction(x['filename'])])) for items in groups.values()], skipped


def group_settings(group, settings, auto_detect=True):
    kind = group[0].get('test_type', settings.get('test_type', 'shaper'))
    result = {**settings, 'test_type': kind}
    if auto_detect:
        result.update(axis_override='auto', measurement_axes=[], test_type_source='recording/filename labels (inferred)')
    if kind == 'belts':
        axes = [axis for item in group for axis in item.get('directions', [])]
        inferred = 'corexy' if sorted(axes) == ['A', 'B'] else 'corexz' if sorted(axes) == ['X', 'Z'] else None
        if inferred is None:
            raise ValueError('A belt set needs exactly A+B or X+Z recordings. Keep the original combined belt file, or use matching session filenames for a split pair.')
        if settings.get('kinematics', 'unknown') not in ('unknown', inferred):
            raise ValueError('Belt labels do not match the selected kinematics. Check the test context.')
        result['kinematics'] = inferred
        result['kinematics_source'] = 'inferred from belt direction labels; confirm test context' if settings.get('kinematics', 'unknown') == 'unknown' else 'user'
    return result
