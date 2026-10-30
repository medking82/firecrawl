"""Validate the private GA401 logical-backup receipt without printing its contents."""
import hashlib
import os
import re
import stat
import time
from pathlib import Path

ROOT = Path('/home/marck/services/firecrawl-security')
FILES = frozenset({
    'firecrawl-nuq-postgres-1-postgres.dump', 'firecrawl-nuq-postgres-1-globals.sql',
    'firecrawl-monitor-app-db-1-postgres.dump', 'firecrawl-monitor-app-db-1-monitor.dump',
    'firecrawl-monitor-app-db-1-globals.sql', 'firecrawl-browser-app-db-1-postgres.dump',
    'firecrawl-browser-app-db-1-browser.dump', 'firecrawl-browser-app-db-1-globals.sql',
})
LINE = re.compile(r'^([0-9a-f]{64}) [ *](.+)$')


def checked_file(path, uid, now, max_age):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError('backup file ownership, mode or type invalid')
    age = now - info.st_mtime
    if age < 0 or age > max_age:
        raise ValueError('backup file outside freshness window')
    return info


def verify_backup(path, max_age, *, root=ROOT, uid=None, now=None):
    if not isinstance(max_age, (int, float)) or isinstance(max_age, bool) or max_age <= 0:
        raise ValueError('positive maximum age required')
    uid = os.getuid() if uid is None else uid
    now = time.time() if now is None else now
    root = Path(root).resolve(strict=True)
    path = Path(path)
    if not path.is_absolute() or path.parent != root or not path.name.startswith('db-backup.'):
        raise ValueError('backup path outside expected root')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError('backup directory ownership, mode or type invalid')
    manifest = path / 'SHA256SUMS'
    checked_file(manifest, uid, now, max_age)
    entries = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        match = LINE.fullmatch(line)
        if not match:
            raise ValueError('invalid backup manifest')
        digest, filename = match.groups()
        target = Path(filename)
        if target.is_absolute():
            if target.parent != path:
                raise ValueError('manifest path outside backup')
            name = target.name
        else:
            if len(target.parts) != 1:
                raise ValueError('manifest path outside backup')
            name = filename
        if name in entries or name not in FILES:
            raise ValueError('unexpected or duplicate backup entry')
        entries[name] = digest
    if set(entries) != FILES:
        raise ValueError('backup manifest incomplete')
    for name in sorted(FILES):
        file = path / name
        checked_file(file, uid, now, max_age)
        h = hashlib.sha256()
        with file.open('rb') as stream:
            if name.endswith('.dump') and stream.read(5) != b'PGDMP':
                raise ValueError('backup is not a custom-format dump')
            stream.seek(0)
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
        if h.hexdigest() != entries[name]:
            raise ValueError('backup checksum mismatch')
    return {'path': str(path), 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'files_verified': len(FILES)}
