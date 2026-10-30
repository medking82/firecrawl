"""Promote three independently patched local images. Run only on the host, after review.

Requires Python 3, Docker Compose, the three candidate image IDs and an idle
browser manager. Failures after admission is paused deliberately leave the gate
in place and never roll back. A private receipt and byte-for-byte backups are
created before changing persistent references.
"""
import argparse
import contextlib
import copy
import hashlib
import http.client
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Pure functions are tested on Windows; promotion requires Linux.
    fcntl = None

UPDATER = Path('/home/marck/.local/state/firecrawl-updater')
BROWSER = Path('/home/marck/services/firecrawl-browser/shared')
MONITOR = Path('/home/marck/services/firecrawl-monitor/shared')
PROJECTS = ('firecrawl', 'firecrawl-monitor', 'firecrawl-browser')
PORTS = {'firecrawl': 3002, 'firecrawl-monitor': 3006, 'firecrawl-browser': 3007}
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')


class Stop(RuntimeError):
    pass


def run(args, *, cwd=None, timeout=40):
    # Never echo subprocess output: Compose errors may include interpolated secrets.
    try:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout,
                                env={k: v for k, v in os.environ.items() if k in
                                     ('PATH', 'HOME', 'USER', 'XDG_RUNTIME_DIR', 'DOCKER_HOST', 'DOCKER_CONFIG')})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop('command unavailable or timed out: ' + args[0]) from exc
    if result.returncode:
        raise Stop('command failed: ' + ' '.join(args[:3]))
    return result.stdout.strip()


def inspect(kind, ref):
    try:
        data = json.loads(run(['docker', kind, 'inspect', ref]))
    except Stop as exc:
        raise Stop('unable to inspect ' + kind + ': ' + ref) from exc
    if len(data) != 1:
        raise Stop('inspect returned an unexpected count')
    return data[0]


def require_id(value):
    if not isinstance(value, str) or not IMAGE_ID.fullmatch(value):
        raise Stop('expected full immutable image ID')
    return value


def checked_candidate(candidate, old, image_lookup):
    require_id(candidate); require_id(old)
    image = image_lookup(candidate)
    if image.get('Id') != candidate or candidate == old:
        raise Stop('candidate image identity invalid')
    labels = image.get('Config', {}).get('Labels') or {}
    if labels.get('org.firecrawl.patch.base-image-id') != old:
        raise Stop('candidate was not built from its current API image')
    base = labels.get('org.firecrawl.patch.base')
    if not base or image_lookup(base).get('Id') != old:
        raise Stop('candidate base label does not resolve to current API image')


def replace_env(raw, updates):
    """Replace only named assignments, preserving all other bytes and line endings."""
    seen = set(); lines = []
    for line in raw.splitlines(keepends=True):
        match = re.match(rb'^([A-Za-z_][A-Za-z0-9_]*)=', line)
        key = match.group(1).decode('ascii') if match else None
        if key in updates:
            if key in seen:
                raise Stop('duplicate runtime.env image assignment')
            seen.add(key)
            ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
            line = key.encode() + b'=' + updates[key].encode('ascii') + ending
        lines.append(line)
    if seen != set(updates):
        raise Stop('missing runtime.env image assignment')
    return b''.join(lines)


def env_value(raw, key):
    matches = [line.split(b'=', 1)[1] for line in raw.splitlines() if line.startswith(key.encode() + b'=')]
    if len(matches) != 1:
        raise Stop('missing or duplicate runtime.env image assignment')
    try: return matches[0].decode('ascii')
    except UnicodeDecodeError as exc: raise Stop('invalid runtime.env image assignment') from exc


def candidate_override(source, service_images, api_image):
    data = copy.deepcopy(source)
    services = data.get('services')
    if not isinstance(services, dict) or 'api' not in services:
        raise Stop('third core compose JSON has no api service')
    for name, image in service_images.items():
        require_id(image)
        service = services.setdefault(name, {})
        if not isinstance(service, dict):
            raise Stop('invalid third compose service')
        service['image'] = api_image if name == 'api' else image
    require_id(api_image)
    return data


def project_snapshot(project):
    ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).splitlines()
    if not ids:
        raise Stop('compose project has no containers: ' + project)
    rows = [inspect('container', item) for item in ids]
    services = {}
    for row in rows:
        labels = row['Config']['Labels'] or {}
        name = labels.get('com.docker.compose.service')
        if not name or name in services or labels.get('com.docker.compose.project') != project:
            raise Stop('unknown or scaled compose service: ' + project)
        services[name] = row
    if 'api' not in services or not services['api']['State']['Running']:
        raise Stop('API container is not running: ' + project)
    labels = services['api']['Config']['Labels']
    paths = labels.get('com.docker.compose.project.config_files', '').split(',')
    workdir = labels.get('com.docker.compose.project.working_dir', '')
    if not workdir or not paths or any(not p or not Path(p).is_absolute() or not Path(p).is_file() for p in paths):
        raise Stop('missing or unsupported compose labels: ' + project)
    if not Path(workdir).is_dir():
        raise Stop('compose working directory missing')
    envfile = labels.get('com.docker.compose.project.environment_file') or None
    if envfile and (',' in envfile or not Path(envfile).is_absolute() or not Path(envfile).is_file()):
        raise Stop('unsupported compose environment file: ' + project)
    return {'project': project, 'services': services, 'paths': paths, 'workdir': workdir, 'envfile': envfile}


def compose(project, files, workdir, envfile, *args):
    cmd = ['docker', 'compose', '--project-name', project]
    if envfile:
        cmd += ['--env-file', str(envfile)]
    for file in files:
        cmd += ['-f', str(file)]
    return run(cmd + list(args), cwd=workdir, timeout=180)


def render(snapshot, files=None, envfile=None):
    return json.loads(compose(snapshot['project'], files or snapshot['paths'], snapshot['workdir'], envfile or snapshot.get('envfile'),
                              'config', '--format', 'json'))


def without_images(config):
    result = copy.deepcopy(config)
    for service in result['services'].values():
        service.pop('image', None)
    return result


def summary(snapshot):
    return {name: {'id': row['Id'], 'image': row['Image'], 'state': row['State'],
                   'mounts': [{k: mount.get(k) for k in ('Type', 'Name', 'Source', 'Destination', 'RW')}
                              for mount in row.get('Mounts', [])]}
            for name, row in snapshot['services'].items()}


def api_configuration(row):
    config = copy.deepcopy(row['Config'])
    for key in ('Image', 'Hostname'): config.pop(key, None)
    if isinstance(config.get('Env'), list):
        values = config['Env']
        keys = [item.split('=', 1)[0] for item in values]
        if len(keys) != len(set(keys)): raise Stop('duplicate environment key')
        config['Env'] = sorted(values)
    labels = config.get('Labels') or {}
    for key in ('com.docker.compose.config-hash', 'com.docker.compose.project.config_files',
                'com.docker.compose.image', 'com.docker.compose.version',
                'com.docker.compose.depends_on',
                'com.docker.compose.replace',
                'org.firecrawl.patch.base', 'org.firecrawl.patch.base-image-id'):
        labels.pop(key, None)
    config['Labels'] = labels
    mounts = sorted([{k: mount.get(k) for k in ('Type', 'Name', 'Source', 'Destination', 'RW')}
                     for mount in row.get('Mounts', [])], key=lambda x:x['Destination'])
    host_config = copy.deepcopy(row.get('HostConfig'))
    if isinstance(host_config,dict) and isinstance(host_config.get('Binds'),list):
        host_config['Binds'] = sorted(host_config['Binds'])
    return {'config': config, 'host_config': host_config, 'mounts': mounts}


def active_sessions():
    count = int(run(['docker', 'exec', 'firecrawl-browser-app-db-1', 'psql', '-X', '-U', 'postgres', '-d',
                     'browser', '-At', '-c', "SELECT count(*) FROM browser_sessions WHERE status='active'"]))
    live = run(['docker', 'ps', '-aq', '--filter', 'label=firecrawl.self-hosted-browser=session'])
    saved = json.loads((BROWSER / 'sessions.json').read_text()) if (BROWSER / 'sessions.json').exists() else {'sessions': []}
    return bool(count or live or any((BROWSER / 'sessions').glob('br_*')) or
                any(s['status'] in ('starting', 'running') for s in saved['sessions']))


def manager_pid():
    pid = int(run(['systemctl', '--user', 'show', 'firecrawl-browser-manager.service', '--value', '--property=MainPID']))
    if pid <= 1:
        raise Stop('browser manager is not running')
    return pid


def freeze(pid):
    os.kill(pid, signal.SIGSTOP)
    for _ in range(100):
        if '\nState:\tT' in Path(f'/proc/{pid}/status').read_text():
            return
        time.sleep(.01)
    raise Stop('browser manager freeze failed')

def start_thaw_watchdog(pid):
    """An independent process resumes the same manager if this process dies."""
    stat_path = Path(f'/proc/{pid}/stat')
    start = stat_path.read_text().rsplit(')', 1)[1].split()[19]
    code = '''import os,signal,sys
pid=int(sys.argv[1]); expected=sys.argv[2]
print('ready', flush=True)
sys.stdin.buffer.read()
try:
 s=open('/proc/%d/stat'%pid).read().rsplit(')',1)[1].split()[19]
 if s==expected: os.kill(pid,signal.SIGCONT)
except (OSError,IndexError): pass
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(pid), start],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    if not select.select([process.stdout], [], [], 5)[0] or process.stdout.readline() != b'ready\n' or process.poll() is not None:
        process.kill(); process.wait(timeout=5)
        raise Stop('manager thaw watchdog failed to start')
    return process


def readiness(port):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request('GET', '/v0/health/readiness')
        response = conn.getresponse(); body = response.read()
        return response.status == 200 and json.loads(body).get('status') == 'ok'
    finally:
        conn.close()


def atomic_write(path, data, mode=0o600, owner=None):
    fd, tmp = tempfile.mkstemp(prefix='.promote-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            if owner is not None: os.fchown(f.fileno(), *owner)
            os.fchmod(f.fileno(), mode); f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


@contextlib.contextmanager
def locks():
    if fcntl is None: raise Stop('promotion requires Linux flock')
    with contextlib.ExitStack() as stack:
        for path in (UPDATER / 'run.lock', UPDATER / 'update.lock', BROWSER / 'manager-activation.lock'):
            f = stack.enter_context(path.open('a'))
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc: raise Stop('lock busy: ' + path.name) from exc
        yield


def promote(candidates, preflight=False, recovery=None):
    recovery = recovery or {}
    os.umask(0o077)
    with locks():
        before = {project: project_snapshot(project) for project in PROJECTS}
        if len(before['firecrawl']['paths']) != 3 or not before['firecrawl']['paths'][2].endswith('.json'):
            raise Stop('expected three core compose files with JSON third')
        for project in PROJECTS:
            checked_candidate(candidates[project], before[project]['services']['api']['Image'], lambda ref: inspect('image', ref))
        envpaths = {'firecrawl-monitor': MONITOR / 'runtime.env', 'firecrawl-browser': BROWSER / 'runtime.env'}
        for p, path in envpaths.items():
            labeled = before[p].get('envfile')
            if labeled and Path(labeled) != path: raise Stop('compose environment file differs from runtime.env: ' + p)
        envdata = {p: path.read_bytes() for p, path in envpaths.items()}
        envstats = {p: path.stat() for p, path in envpaths.items()}
        for path in envpaths.values():
            if hasattr(os, 'listxattr') and os.listxattr(path): raise Stop('runtime.env has unsupported extended attributes')
        for p, raw in envdata.items():
            current_core = inspect('image', env_value(raw, 'BASE_API'))['Id']
            key = 'MONITOR_IMAGE' if p == 'firecrawl-monitor' else 'BROWSER_API_IMAGE'
            current_overlay = inspect('image', env_value(raw, key))['Id']
            if current_core != before['firecrawl']['services']['api']['Image'] or current_overlay != before[p]['services']['api']['Image']:
                raise Stop('persistent image references disagree with current containers: ' + p)
        envnew = {}
        for p in envpaths:
            updates = {'BASE_API': candidates['firecrawl'],
                       ('MONITOR_IMAGE' if p == 'firecrawl-monitor' else 'BROWSER_API_IMAGE'): candidates[p]}
            if recovery:
                postgres = require_id(recovery['nuq-postgres'])
                if env_value(envdata[p], 'POSTGRES_IMAGE') != before['firecrawl']['services']['nuq-postgres']['Image']:
                    raise Stop('postgres reference differs from running core image')
                updates['POSTGRES_IMAGE'] = postgres
                if p == 'firecrawl-browser':
                    old_browser = before['firecrawl']['services']['playwright-service']['Image']
                    if env_value(envdata[p], 'BROWSER_IMAGE') != old_browser:
                        raise Stop('browser image reference differs from running playwright image')
                    updates['BROWSER_IMAGE'] = require_id(recovery['playwright-service'])
            envnew[p] = replace_env(envdata[p], updates)
        manager_unit = Path('/home/marck/.config/systemd/user/firecrawl-browser-manager.service')
        manager_before = manager_unit.read_bytes() if recovery else None
        manager_after = None
        if recovery:
            old_line = ('Environment=BROWSER_IMAGE=' + old_browser).encode()
            new_line = ('Environment=BROWSER_IMAGE=' + recovery['playwright-service']).encode()
            if manager_before.splitlines().count(old_line) != 1:
                raise Stop('browser manager image reference changed')
            manager_after = manager_before.replace(old_line, new_line, 1)
        source_path = Path(before['firecrawl']['paths'][2])
        source = json.loads(source_path.read_bytes())
        rendered = {p: render(before[p], envfile=envpaths.get(p)) for p in PROJECTS}
        images = {}
        for name, service in rendered['firecrawl']['services'].items():
            ref = service.get('image')
            if not ref: raise Stop('core service lacks an image: ' + name)
            if name in recovery:
                require_id(recovery[name]); inspect('image', recovery[name])
                if ref != before['firecrawl']['services'][name]['Image']:
                    raise Stop('recovery preimage reference changed: ' + name)
                resolved = recovery[name]
            else:
                resolved = inspect('image', ref)['Id']; require_id(resolved)
            if name not in recovery and name in before['firecrawl']['services'] and before['firecrawl']['services'][name]['Image'] != resolved:
                raise Stop('core image reference differs from current container: ' + name)
            images[name] = resolved
        if set(source.get('services', {})) - set(images):
            raise Stop('third core compose JSON contains inactive or unknown services')
        override = candidate_override(source, images, candidates['firecrawl'])
        receipt_dir = UPDATER / ('patch-promotion-' + uuid.uuid4().hex)
        receipt_dir.mkdir(mode=0o700)
        candidate_path = source_path.with_name(source_path.stem + '.candidate-' + receipt_dir.name + '.json')
        record = {'status': 'preparing', 'candidates': candidates, 'projects': {}, 'candidate_file': str(candidate_path)}
        def receipt(): atomic_write(receipt_dir / 'receipt.json', json.dumps(record, indent=2).encode())
        for p in PROJECTS:
            snap = before[p]
            record['projects'][p] = {'compose_files': snap['paths'], 'workdir': snap['workdir'], 'before': summary(snap)}
            for i, file in enumerate(snap['paths']):
                data = Path(file).read_bytes(); atomic_write(receipt_dir / f'{p}-compose-{i}', data)
                record['projects'][p].setdefault('file_sha256', {})[file] = hashlib.sha256(data).hexdigest()
        core_env = Path(before['firecrawl'].get('envfile') or (Path(before['firecrawl']['workdir']) / '.env'))
        if core_env.exists():
            data = core_env.read_bytes(); atomic_write(receipt_dir / 'firecrawl-dotenv', data)
            record['projects']['firecrawl']['dotenv'] = {'path': str(core_env), 'sha256': hashlib.sha256(data).hexdigest()}
        for p, data in envdata.items():
            atomic_write(receipt_dir / (p + '-runtime.env'), data)
            record['projects'][p]['runtime_env_sha256_before'] = hashlib.sha256(data).hexdigest()
            st = envstats[p]
            record['projects'][p]['runtime_env_metadata'] = {'mode': stat.S_IMODE(st.st_mode), 'uid': st.st_uid, 'gid': st.st_gid}
        if recovery:
            atomic_write(receipt_dir / 'browser-manager.service', manager_before)
            record['browser_manager_sha256_before'] = hashlib.sha256(manager_before).hexdigest()
        receipt()
        # Create exclusive candidate; no existing config is overwritten.
        with candidate_path.open('xb') as f:
            f.write(json.dumps(override, indent=2).encode()); f.flush(); os.fsync(f.fileno())
        os.chmod(candidate_path, 0o600)
        candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        record['candidate_sha256'] = candidate_hash; receipt()
        candidate_files = before['firecrawl']['paths'][:2] + [str(candidate_path)]
        with tempfile.TemporaryDirectory(dir=receipt_dir) as tmp:
            proposed_env = {}
            for p, data in envnew.items():
                proposed_env[p] = Path(tmp) / (p + '.env'); proposed_env[p].write_bytes(data)
            proposed = {'firecrawl': render(before['firecrawl'], candidate_files)}
            for p in envpaths: proposed[p] = render(before[p], envfile=proposed_env[p])
            for p in PROJECTS:
                if without_images(rendered[p]) != without_images(proposed[p]):
                    raise Stop('candidate changes compose configuration beyond images: ' + p)
                for name, service in proposed[p]['services'].items():
                    if name == 'api': expected = candidates[p]
                    elif p == 'firecrawl' and name in recovery: expected = recovery[name]
                    elif p in envpaths and name == 'app-db' and recovery: expected = recovery['nuq-postgres']
                    else: expected = inspect('image', rendered[p]['services'][name]['image'])['Id']
                    if inspect('image', service['image'])['Id'] != expected:
                        raise Stop('candidate changes another service image: ' + p)
        if preflight:
            record['status'] = 'preflight-passed'; receipt()
            print(json.dumps({'status': 'preflight-passed', 'receipt': str(receipt_dir / 'receipt.json')}))
            return
        gate = BROWSER / 'admission-paused'
        if active_sessions(): raise Stop('browser sessions active')
        pid = manager_pid(); frozen = False; paused = False; success = False; watchdog = None
        try:
            with gate.open('x') as f: f.write(receipt_dir.name)
            paused = True; record['status'] = 'admission-paused'; receipt()
            if manager_pid() != pid: raise Stop('manager PID changed')
            watchdog = start_thaw_watchdog(pid)
            frozen = True; freeze(pid)
            if active_sessions(): raise Stop('browser sessions active after manager freeze')
            if recovery and manager_unit.read_bytes() != manager_before:
                raise Stop('browser manager unit changed during preflight')
            # Guard against configuration changes between review and mutation.
            for p in PROJECTS:
                if summary(project_snapshot(p)) != summary(before[p]): raise Stop('containers changed during preflight: ' + p)
                for file, digest in record['projects'][p]['file_sha256'].items():
                    if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest: raise Stop('compose source changed during preflight: ' + p)
                checked_candidate(candidates[p], before[p]['services']['api']['Image'], lambda ref: inspect('image', ref))
            if core_env.exists() and hashlib.sha256(core_env.read_bytes()).hexdigest() != record['projects']['firecrawl'].get('dotenv', {}).get('sha256'):
                raise Stop('core dotenv changed during preflight')
            for p, path in envpaths.items():
                if path.read_bytes() != envdata[p]: raise Stop('runtime.env changed during preflight')
            for p, path in envpaths.items():
                st = envstats[p]
                if path.stat() != st: raise Stop('runtime.env metadata changed during preflight: ' + p)
                atomic_write(path, envnew[p], stat.S_IMODE(st.st_mode), (st.st_uid, st.st_gid))
                record['projects'][p]['runtime_env_sha256_after'] = hashlib.sha256(envnew[p]).hexdigest()
                record['status'] = 'updated-' + p; receipt()
            for p in PROJECTS:
                actual = render(before[p], candidate_files if p == 'firecrawl' else None, envpaths.get(p))
                if actual != proposed[p]:
                    raise Stop('effective compose configuration changed after preflight: ' + p)
            if recovery:
                st = manager_unit.stat()
                atomic_write(manager_unit, manager_after, stat.S_IMODE(st.st_mode), (st.st_uid, st.st_gid))
                record['status'] = 'updating-browser-manager'; receipt()
                run(['systemctl', '--user', 'daemon-reload'])
                os.kill(pid, signal.SIGKILL); frozen = False
                run(['systemctl', '--user', 'restart', 'firecrawl-browser-manager.service'], timeout=90)
                if run(['systemctl', '--user', 'is-active', 'firecrawl-browser-manager.service']) != 'active':
                    raise Stop('browser manager failed to restart')
                pid = manager_pid()
                if active_sessions(): raise Stop('browser session appeared during manager restart')
            for p in PROJECTS:
                files = candidate_files if p == 'firecrawl' else before[p]['paths']
                if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_hash:
                    raise Stop('candidate compose file changed')
                record['status'] = 'recreating-' + p; receipt()
                compose(p, files, before[p]['workdir'], envpaths.get(p) or before[p].get('envfile'), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'api')
                current = project_snapshot(p)
                record['projects'][p]['after'] = summary(current); receipt()
                if current['services']['api']['Image'] != candidates[p] or not current['services']['api']['State']['Running']:
                    raise Stop('candidate image compare failed: ' + p)
                if api_configuration(current['services']['api']) != api_configuration(before[p]['services']['api']):
                    raise Stop('API container configuration changed: ' + p)
                deadline = time.monotonic() + 180
                while True:
                    try:
                        if readiness(PORTS[p]): break
                    except (OSError, http.client.HTTPException): pass
                    if time.monotonic() >= deadline: raise Stop('readiness failed: ' + p)
                    time.sleep(2)
                record['projects'][p]['after'] = summary(current); record['status'] = 'ready-' + p; receipt()
            for p in PROJECTS:
                after = project_snapshot(p)
                old = summary(before[p]); new = summary(after)
                old.pop('api'); new.pop('api')
                if old != new: raise Stop('non-API container changed: ' + p)
                if after['services']['api']['Image'] != candidates[p]: raise Stop('final image compare failed: ' + p)
                if not readiness(PORTS[p]): raise Stop('final readiness failed: ' + p)
                for file, digest in record['projects'][p]['file_sha256'].items():
                    if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest: raise Stop('compose source changed: ' + p)
            if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_hash:
                raise Stop('candidate compose file changed')
            if core_env.exists() and hashlib.sha256(core_env.read_bytes()).hexdigest() != record['projects']['firecrawl'].get('dotenv', {}).get('sha256'):
                raise Stop('core dotenv changed')
            for p, path in envpaths.items():
                if path.read_bytes() != envnew[p]: raise Stop('runtime.env changed after update: ' + p)
            if manager_pid() != pid: raise Stop('manager PID changed during promotion')
            if active_sessions(): raise Stop('browser session appeared during promotion')
            record['status'] = 'ready-admission-paused'; receipt(); success = True
        except Exception as exc:
            record['failure_stage'] = record['status']
            record['failure'] = str(exc) if isinstance(exc, Stop) else type(exc).__name__
            record['status'] = 'failed-manual-intervention'; receipt()
            raise
        finally:
            if frozen:
                try: os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    record['status'] = 'failed-manager-missing'; receipt()
                    raise Stop('manager disappeared; admission remains paused')
            if watchdog:
                watchdog.stdin.close()
                watchdog.wait(timeout=5)
            if success and paused:
                if gate.read_text() != receipt_dir.name: raise Stop('admission gate ownership changed')
                gate.unlink()
        record['status'] = 'complete'; receipt()
        print(json.dumps({'status': 'complete', 'receipt': str(receipt_dir / 'receipt.json')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-image', required=True); parser.add_argument('--monitor-image', required=True)
    parser.add_argument('--browser-image', required=True)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--postgres-recovery-image')
    parser.add_argument('--playwright-recovery-image')
    args = parser.parse_args()
    recovery = {}
    if args.postgres_recovery_image or args.playwright_recovery_image:
        if not args.postgres_recovery_image or not args.playwright_recovery_image:
            parser.error('both recovery images are required')
        recovery = {'nuq-postgres': args.postgres_recovery_image, 'playwright-service': args.playwright_recovery_image}
    promote(dict(zip(PROJECTS, (args.core_image, args.monitor_image, args.browser_image))), preflight=args.preflight, recovery=recovery)


if __name__ == '__main__':
    def terminate(_signum, _frame):
        raise Stop('promotion interrupted')
    signal.signal(signal.SIGTERM, terminate)
    try: main()
    except Exception as exc:
        # Do not print exception text from Docker, JSON or runtime.env.
        print('promotion stopped: ' + (str(exc) if isinstance(exc, Stop) else type(exc).__name__) + '; inspect private receipt; no automatic rollback', file=sys.stderr)
        sys.exit(1)
