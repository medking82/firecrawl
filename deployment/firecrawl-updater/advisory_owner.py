"""Daily owner for three running Firecrawl API images."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid

import advisory_trivy as scanner

TARGETS = ('firecrawl-api-1', 'firecrawl-monitor-api-1', 'firecrawl-browser-api-1')
MAX_AGE = dt.timedelta(hours=48)
MAX_FUTURE = dt.timedelta(minutes=5)
GENERATION = re.compile(r'^gen-[0-9a-f]{32}$')


def private_dir(path, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise scanner.ScanError('directory is not private and owned by scan user')
    return path


def timestamp(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise scanner.ScanError('invalid DB timestamp')
    try:
        return dt.datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError as exc:
        raise scanner.ScanError('invalid DB timestamp') from exc


def metadata(path, version, now):
    data = scanner.parse_json(path.read_bytes())
    if not isinstance(data, dict) or type(data.get('Version')) is not int or data['Version'] != version:
        raise scanner.ScanError('invalid DB metadata version')
    updated, next_update, downloaded = (timestamp(data.get(k)) for k in ('UpdatedAt', 'NextUpdate', 'DownloadedAt'))
    if updated > now + MAX_FUTURE or downloaded > now + MAX_FUTURE or updated > downloaded + MAX_FUTURE:
        raise scanner.ScanError('future or inconsistent DB metadata')
    if now - updated > MAX_AGE or next_update <= now or next_update <= updated:
        raise scanner.ScanError('stale DB metadata')
    return {'version': version, 'updated_at': data['UpdatedAt'], 'next_update': data['NextUpdate'], 'downloaded_at': data['DownloadedAt']}


def stage_cache(cache, dest, now):
    private_dir(cache)
    evidence = {}
    for sub, filename, version in (('db', 'trivy.db', 2),):
        source = private_dir(cache / sub)
        target = dest / sub
        target.mkdir(mode=0o700)
        for name in ('metadata.json', filename):
            src = source / name
            info = src.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size == 0:
                raise scanner.ScanError('DB cache file is not private or is empty')
            if name == 'metadata.json' and info.st_size > 100_000:
                raise scanner.ScanError('DB metadata too large')
            shutil.copyfile(src, target / name, follow_symlinks=False)
            os.chmod(target / name, 0o600)
        evidence[sub] = metadata(target / 'metadata.json', version, now)
    return evidence


def owned_generation(directory):
    """Accept only the private, exact structure created by this owner."""
    if not GENERATION.fullmatch(directory.name):
        raise scanner.ScanError('invalid generation name')
    private_dir(directory)
    if {p.name for p in directory.iterdir()} != {'db', '.owner-generation'}:
        raise scanner.ScanError('unexpected generation contents')
    marker = directory / '.owner-generation'
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 256:
        raise scanner.ScanError('invalid generation marker')
    if marker.read_text(encoding='ascii') != directory.name + '\n':
        raise scanner.ScanError('invalid generation marker')
    db = private_dir(directory / 'db')
    if {p.name for p in db.iterdir()} != {'metadata.json', 'trivy.db'}:
        raise scanner.ScanError('unexpected DB generation contents')
    for name in ('metadata.json', 'trivy.db'):
        info = (db / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size == 0:
            raise scanner.ScanError('invalid DB generation file')


def current_generation(cache, generations):
    current = cache / 'current'
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise scanner.ScanError('invalid current generation pointer')
    link = os.readlink(current)
    if not re.fullmatch(r'generations/gen-[0-9a-f]{32}', link):
        raise scanner.ScanError('invalid current generation pointer')
    previous = generations / link.split('/')[1]
    owned_generation(previous)
    return previous.name


def retain_generations(generations, keep):
    # Never resolve or follow links. Unknown entries remain untouched; suspicious
    # generation names block the scan instead of risking arbitrary deletion.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise scanner.ScanError('safe generation deletion unavailable')
    for entry in generations.iterdir():
        if not GENERATION.fullmatch(entry.name) or entry.name in keep:
            continue
        owned_generation(entry)
        shutil.rmtree(entry)


def refresh_cache(cache, execute=scanner.run, now=None):
    """Refresh without mounting images or credentials; publish only a validated DB."""
    cache = private_dir(cache, create=True)
    generations = private_dir(cache / 'generations', create=True)
    previous = current_generation(cache, generations)
    pinned = scanner.parse_json(execute(['docker', 'image', 'inspect', '--format', '{{json .Id}}', scanner.TRIVY]))
    if pinned != scanner.TRIVY:
        raise scanner.ScanError('Trivy image identity mismatch')
    with tempfile.TemporaryDirectory(prefix='.refresh-', dir=generations) as temporary:
        execute(['docker', 'run', '--rm', '--pull=never', '--read-only', '--cap-drop=ALL',
                 '--security-opt=no-new-privileges', '--pids-limit=256', '--user',
                 f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,nosuid,nodev,size=1g',
                 '--mount', f'type=bind,src={temporary},dst=/cache', scanner.TRIVY,
                 '--config', '/dev/null', '--cache-dir', '/cache', 'image',
                 '--download-db-only', '--timeout', '15m'])
        source = Path(temporary) / 'db'
        if not source.is_dir() or source.is_symlink():
            raise scanner.ScanError('refresh did not produce DB directory')
        os.chmod(source, 0o700)
        for name in ('metadata.json', 'trivy.db'):
            p = source / name
            if p.is_symlink() or not p.is_file():
                raise scanner.ScanError('refresh did not produce DB files')
            os.chmod(p, 0o600)
        metadata(source / 'metadata.json', 2, now or dt.datetime.now(dt.timezone.utc))
        if (source / 'trivy.db').stat().st_size == 0:
            raise scanner.ScanError('refreshed DB is empty')
        # Promote a complete generation; current changes only after validation.
        name = 'gen-' + uuid.uuid4().hex
        generation = generations / name
        marker = Path(temporary) / '.owner-generation'
        marker.write_text(name + '\n', encoding='ascii')
        os.chmod(marker, 0o600)
        os.rename(temporary, generation)
        owned_generation(generation)
        link = cache / ('.current-' + uuid.uuid4().hex)
        try:
            os.symlink('generations/' + name, link)
            os.replace(link, cache / 'current')
        finally:
            if link.is_symlink():
                link.unlink()
        retain_generations(generations, {name, previous})
        return generation


def offline_executor(stage, execute):
    def invoke(command):
        if command[:2] != ['docker', 'run']:
            return execute(command)
        command = command.copy()
        command[2:2] = ['--network=none', '--user', f'{os.getuid()}:{os.getgid()}',
                        '--mount', f'type=bind,src={stage},dst=/cache']
        index = command.index('--cache-dir')
        command[index + 1] = '/cache'
        command.extend(['--skip-db-update', '--skip-java-db-update', '--offline-scan'])
        output = execute(command)
        document = scanner.parse_json(output)
        results = document.get('Results') if isinstance(document, dict) else None
        if isinstance(results, list) and any(isinstance(r, dict) and str(r.get('Type', '')).lower() in {'jar', 'java', 'pom', 'maven', 'gradle'} for r in results):
            raise scanner.ScanError('Java image needs a reviewed Java DB workflow')
        return output
    return invoke


def atomic_report(directory, data):
    encoded = (json.dumps(data, sort_keys=True) + '\n').encode()
    fd, name = tempfile.mkstemp(prefix='.report-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, directory / 'latest.json')
        dirfd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def perform(cache, execute=scanner.run, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    generation = refresh_cache(cache, execute, now)
    captures = {name: scanner.capture('running', name, execute) for name in TARGETS}
    with tempfile.TemporaryDirectory(prefix='firecrawl-advisory-cache-') as tmp:
        stage = Path(tmp)
        db = stage_cache(generation, stage, now)
        offline = offline_executor(stage, execute)
        results = []
        for name in TARGETS:
            result = scanner.scan('running', name, execute=offline)
            if result['image_id'] != captures[name][0]:
                raise scanner.ScanError('running image changed during scan')
            results.append({'container': name, **result})
        if any(scanner.capture('running', name, execute) != captures[name] for name in TARGETS):
            raise scanner.ScanError('running container changed during scan')
        for sub, version in (('db', 2),):
            metadata(stage / sub / 'metadata.json', version, dt.datetime.now(dt.timezone.utc))
    count = sum(len(item['findings']) for item in results)
    return {'status': 'findings' if count else 'no_high_critical_findings',
            'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'db': db, 'images': results, 'findings_count': count}


def main():
    # DB refresh has network access; image scans are offline, with no notifications.
    import fcntl
    os.umask(0o077)
    state = private_dir(Path.home() / '.local/state/firecrawl-advisory', create=True)
    reports = private_dir(state / 'reports', create=True)
    with open(state / 'scan.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('scan already running', file=sys.stderr)
            return 2
        try:
            result = perform(state / 'cache')
            code = 1 if result['findings_count'] else 0
        except Exception:
            result = {'status': 'error', 'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'error': 'scan incomplete; check local cache, Docker and scanner availability'}
            code = 2
        try:
            atomic_report(reports, result)
        except OSError:
            print('unable to publish report', file=sys.stderr)
            return 2
        print(json.dumps({'status': result['status'], 'findings_count': result.get('findings_count')}))
        return code


if __name__ == '__main__':
    sys.exit(main())
