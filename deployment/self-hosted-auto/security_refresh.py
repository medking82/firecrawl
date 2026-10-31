"""Image-only Debian security refresh. Source upgrades remain a separate admission path."""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path.home() / '.local/state/firecrawl-security-refresh'
PROJECTS = ('firecrawl', 'firecrawl-monitor', 'firecrawl-browser')
HELPER = Path('/home/marck/services/firecrawl-browser/releases/v629-991c7413-e36e048a5aec8533/deployment/patched-base/promote.py')
HELPER_SHA = '9585cac5d1749169b4ac05d4049b310557c545898622c8b95b83b437cc66ea1c'
FORBIDDEN = {'CVE-2026-111497'}


def run(args, timeout=900):
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode: raise RuntimeError('command failed: ' + ' '.join(args[:3]))
    return r.stdout.decode().strip()


def save(path, value):
    """Commit the journal before mutation, including the directory entry."""
    temp = path.with_name(path.name + '.tmp')
    with open(temp, 'w', encoding='utf-8') as f:
        os.chmod(temp, 0o600); json.dump(value, f); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def status_report(value):
    save(ROOT / 'latest.json', {**value, 'completed_at': dt.datetime.now(dt.timezone.utc).isoformat()})


def normalize_api_configuration(value):
    """Docker 29 reports empty DNS lists as null after recreate; same runtime."""
    host = value.get('host_config')
    if isinstance(host, dict):
        for key in ('Dns', 'DnsOptions', 'DnsSearch'):
            if key in host and host[key] is None: host[key] = []
    return value


def helper():
    if HELPER.is_symlink() or hashlib.sha256(HELPER.read_bytes()).hexdigest() != HELPER_SHA:
        raise RuntimeError('deployment helper changed')
    spec = importlib.util.spec_from_file_location('refresh_promote', HELPER)
    p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
    raw = p.api_configuration
    p.api_configuration = lambda row: normalize_api_configuration(raw(row))
    return p


def identity(p, project):
    row = p.project_snapshot(project)['services']['api']
    return row['Id'], row['Image']


def recovery_image(project):
    ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project,
               '--filter', 'label=com.docker.compose.service=api']).splitlines()
    if len(ids) > 1: raise RuntimeError('ambiguous recovery API')
    if not ids: return None
    return json.loads(run(['docker', 'inspect', ids[0]]))[0]['Image']


def verify_runtime(p, project, snapshot):
    ids = run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project]).splitlines()
    rows = [p.inspect('container', cid) for cid in ids]
    services = {}
    for row in rows:
        labels = row['Config']['Labels'] or {}
        name = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project') != project or not name or name in services:
            raise RuntimeError('ambiguous container ownership')
        services[name] = row
    if set(services) - set(snapshot['services']): raise RuntimeError('unknown project service')
    for name, original in snapshot['services'].items():
        current = services.get(name)
        if name == 'api':
            if current and p.api_configuration(current) != p.api_configuration(original):
                raise RuntimeError('API runtime configuration drift before mutation')
        elif not current or (current['Id'],current['Image']) != (original['Id'],original['Image']):
            raise RuntimeError('non-API identity drift before mutation')


def release_gate(p, journal):
    gate = p.BROWSER / 'admission-paused'
    if gate.exists():
        if json.loads(gate.read_text()) != {'owner': str(journal)}:
            raise RuntimeError('browser gate ownership changed')
        gate.unlink()


def idle(p):
    query = "SELECT (SELECT count(*) FROM nuq.queue_scrape WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_crawl_finished WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_scrape_backlog)"
    for container in ('firecrawl-nuq-postgres-1','firecrawl-monitor-app-db-1','firecrawl-browser-app-db-1'):
        if p.run(['docker','exec',container,'psql','-X','-U','postgres','-d','postgres','-v','ON_ERROR_STOP=1','-At','-c',query],timeout=30) != '0':
            raise RuntimeError('active queue; update deferred')
    checks = "SELECT count(*) FROM public.monitor_checks WHERE status IN ('queued','running')"
    if p.run(['docker','exec','firecrawl-monitor-app-db-1','psql','-X','-U','postgres','-d','monitor','-v','ON_ERROR_STOP=1','-At','-c',checks],timeout=30) != '0' or p.active_sessions():
        raise RuntimeError('monitor or browser active; update deferred')
    for port in p.PORTS.values():
        if p.run(['ss','-H','-tn','state','established','( sport = :'+str(port)+' )'],timeout=10):
            raise RuntimeError('active API connections; update deferred')


def ready(p, project):
    for _ in range(90):
        try:
            if p.readiness(p.PORTS[project]): return
        except (OSError, ValueError):
            # Node can accept/reset TCP while workers initialize after recreate.
            pass
        time.sleep(2)
    raise RuntimeError('readiness failed: ' + project)


def candidate_policy(baseline, candidate):
    # Ignore distro display version and installed package version: an existing CVE
    # remains existing after a partial vendor fix. Severity escalation is new.
    def key(f):
        target = '' if f['type'] == 'debian' else f['target']
        return (target, f['type'], f['id'], f['package'], f['severity'])
    old = {key(f) for f in baseline}
    if any(f['id'] in FORBIDDEN for f in candidate):
        raise RuntimeError('previously fixed Brotli CVE returned')
    added = [f for f in candidate if key(f) not in old]
    if added: raise RuntimeError('candidate introduces HIGH/CRITICAL findings')


def package_floor(image):
    run(['docker', 'run', '--rm', '--network=none', '--entrypoint', 'sh', image, '-ec',
         '. /etc/os-release; test "$ID:$VERSION_ID" = debian:12; '
         'v=$(dpkg-query -W -f=\'${Version}\' libbrotli1); '
         'dpkg --compare-versions "$v" ge 1.0.9-2+deb12u1'])


def packages(image):
    return run(['docker','run','--rm','--network=none','--entrypoint','dpkg-query',image,'-W'])


def build(image, directory, project):
    config = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    if config['Config'].get('OnBuild'): raise RuntimeError('ONBUILD unsupported')
    if config['Config'].get('User') not in ('', None, 'root', '0'):
        raise RuntimeError('security refresh supports reviewed root images only')
    tag = 'firecrawl-security-refresh:base-' + uuid.uuid4().hex
    run(['docker', 'tag', image, tag])
    context = directory / project; context.mkdir(mode=0o700)
    (context / 'Dockerfile').write_text(
        'FROM ' + tag + '\nRUN printf "%s\\n" '
        '"deb https://deb.debian.org/debian-security bookworm-security main" > /tmp/security.list '
        '&& apt-get -o APT::Update::Error-Mode=any -o Dir::Etc::sourcelist=/tmp/security.list -o Dir::Etc::sourceparts=- update '
        '&& DEBIAN_FRONTEND=noninteractive apt-get -y -o Dir::Etc::sourcelist=/tmp/security.list '
        '-o Dir::Etc::sourceparts=- upgrade '
        '&& rm -rf /var/lib/apt/lists/* /tmp/security.list\n')
    output = context / 'iid'
    try:
        env = dict(os.environ, DOCKER_BUILDKIT='0')
        r = subprocess.run(['docker', 'build', '--pull=false', '--iidfile', str(output), str(context)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1200)
        if r.returncode: raise RuntimeError('security image build failed: ' + project)
        new = output.read_text().strip()
    finally:
        # A running untagged base can gain this sole temporary tag. Ordinary
        # rmi then refuses to remove it; force removes only our temporary ref.
        run(['docker', 'image', 'rm', '--no-prune', '--force', tag])
    patched = json.loads(run(['docker', 'image', 'inspect', new]))[0]
    # USER root and USER <original> can canonicalize empty user to root.
    expected = dict(config['Config']); actual = dict(patched['Config'])
    expected['User'] = expected.get('User') or 'root'; actual['User'] = actual.get('User') or 'root'
    if actual != expected or patched['RootFS']['Layers'][:len(config['RootFS']['Layers'])] != config['RootFS']['Layers']:
        raise RuntimeError('image configuration or ancestry changed')
    package_floor(new)
    verifier = HELPER.parent / 'verify-transport.py'
    if hashlib.sha256(verifier.read_bytes()).hexdigest() != '558cb5d6354dcdbf7394e70d0f8130e9e43ee5c0fb312cf4b55dd6504f3d4f69':
        raise RuntimeError('transport verifier changed')
    guard = Path.home() / '.local/state/firecrawl-updater/guards/07f73956c9531c483ccd9816af9b3aa06de87424dc0ab33758e9daa2c5e16c3d.py'
    run([sys.executable, '-B', str(verifier), new, str(guard), guard.stem])
    return new


def check_files(record):
    for path, digest in record['files'].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise RuntimeError('deployment input changed: ' + path)


def apply_image(p, record, project, image, directory, suffix):
    check_files(record)
    snap = record['snapshots'][project]
    verify_runtime(p, project, snap)
    overlay = directory / (project + '-' + suffix + '.json')
    save(overlay, {'services': {'api': {'image': image}}})
    p.compose(project, snap['paths'] + [str(overlay)], snap['workdir'], snap.get('envfile'),
              'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'api')
    if identity(p, project)[1] != image: raise RuntimeError('deployed image mismatch')
    after = p.project_snapshot(project)
    if p.api_configuration(after['services']['api']) != p.api_configuration(snap['services']['api']):
        raise RuntimeError('API runtime configuration changed')
    for name, row in snap['services'].items():
        if name != 'api' and (after['services'][name]['Id'], after['services'][name]['Image']) != (row['Id'], row['Image']):
            raise RuntimeError('non-API service changed')
    ready(p, project)


def recover(p, journal):
    record = json.loads(journal.read_text())
    if record['status'] in ('prepared', 'gated', 'committed', 'rolled-back'):
        # A crash can occur between journal commit and gate release.
        gate = p.BROWSER / 'admission-paused'
        if gate.exists() and json.loads(gate.read_text()) == {'owner': str(journal)}:
            if record['status'] not in ('prepared','gated'):
                expected = record['new'] if record['status'] == 'committed' else record['old']
                check_files(record)
                for project in PROJECTS:
                    verify_runtime(p, project, record['snapshots'][project])
                    if recovery_image(project) != expected[project]: raise RuntimeError('final image changed before gate release')
                    ready(p, project)
            release_gate(p, journal)
        return
    if record['status'] not in ('applying', 'recovering'): return
    check_files(record)
    gate = p.BROWSER / 'admission-paused'
    if not gate.is_file() or json.loads(gate.read_text()) != {'owner':str(journal)}:
        raise RuntimeError('missing or foreign recovery gate')
    for project in PROJECTS: verify_runtime(p, project, record['snapshots'][project])
    record['status'] = 'recovering'; save(journal, record)
    for project in PROJECTS:
        if recovery_image(project) not in (None, record['old'][project], record['new'][project]):
            raise RuntimeError('unknown image during recovery: ' + project)
    for project in PROJECTS:
        apply_image(p, record, project, record['old'][project], journal.parent, 'recovery')
    record['status'] = 'rolled-back'; save(journal, record)
    release_gate(p, journal)


def update(p, root, prepare_only=False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'firecrawl-updater'))
    import advisory_owner as owner
    import advisory_trivy as scanner
    # One fresh, isolated DB snapshot for every baseline/candidate comparison.
    cache = root / 'cache'
    generation = owner.refresh_cache(cache)
    directory = root / ('run-' + uuid.uuid4().hex); directory.mkdir(mode=0o700)
    snapshots = {k: p.project_snapshot(k) for k in PROJECTS}
    old = {k: snapshots[k]['services']['api']['Image'] for k in PROJECTS}
    new = {}; checks = {}
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        stage = Path(temporary)
        db = owner.stage_cache(generation, stage, dt.datetime.now(dt.timezone.utc))
        execute = owner.offline_executor(stage, scanner.run)
        for project in PROJECTS:
            package_floor(old[project])
            new[project] = build(old[project], directory, project)
            if packages(new[project]) == packages(old[project]):
                unused = new[project]; new[project] = old[project]
                if unused != old[project]:
                    try: run(['docker','image','rm',unused])
                    except RuntimeError: pass  # Retain a referenced build artifact.
            baseline = scanner.scan('running', project + '-api-1', execute)
            candidate = scanner.scan('candidate', new[project], execute)
            candidate_policy(baseline['findings'], candidate['findings'])
            checks[project] = {'before': len(baseline['findings']), 'after': len(candidate['findings'])}
    files = {}
    for snap in snapshots.values():
        inputs = snap['paths'] + ([snap['envfile']] if snap.get('envfile') else [])
        default_env = Path(snap['workdir']) / '.env'
        if default_env.is_file(): inputs.append(str(default_env))
        for path in inputs:
            files[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    record = {'status': 'prepared', 'snapshots': snapshots, 'old': old, 'new': new, 'files': files,
              'database': db, 'checks': checks, 'prepared_at': time.time()}
    journal = directory / 'transaction.json'; save(journal, record)
    if prepare_only:
        print(str(journal), flush=True)
        return
    promote(p, journal)


def promote(p, journal):
    record = json.loads(journal.read_text()); directory = journal.parent
    if record['status'] != 'prepared' or not 0 <= time.time() - record['prepared_at'] <= 3600:
        raise RuntimeError('candidate is not freshly prepared')
    now = dt.datetime.now(dt.timezone.utc)
    for meta in record['database'].values():
        updated = dt.datetime.fromisoformat(meta['updated_at'].replace('Z','+00:00'))
        expiry = dt.datetime.fromisoformat(meta['next_update'].replace('Z','+00:00'))
        if expiry <= now or now - updated > dt.timedelta(hours=48):
            raise RuntimeError('candidate vulnerability database is stale')
    snapshots, old, new = record['snapshots'], record['old'], record['new']
    for image in new.values(): package_floor(image)
    # Stable inputs and idleness are rechecked immediately before the stop barrier.
    for project in PROJECTS:
        if identity(p, project) != (snapshots[project]['services']['api']['Id'], old[project]):
            raise RuntimeError('running API changed during preparation')
        verify_runtime(p, project, snapshots[project])
    check_files(record)
    if new == old:
        for project in PROJECTS: ready(p, project)
        record['status'] = 'committed'; save(journal, record)
        status_report({'status':'already-current','checks':record['checks'],'transaction':str(journal)})
        return
    idle(p)
    if p.active_sessions(): raise RuntimeError('browser sessions active; deferred')
    gate = p.BROWSER / 'admission-paused'
    if gate.exists(): raise RuntimeError('browser admission already paused')
    save(gate, {'owner': str(journal)})
    record['status'] = 'gated'; save(journal, record)
    watchdog = None; pid = None
    try:
        pid = p.manager_pid(); watchdog = p.start_thaw_watchdog(pid); p.freeze(pid)
        if p.active_sessions(): raise RuntimeError('browser session raced with admission gate')
        record['status'] = 'applying'; save(journal, record)
        # A stop barrier closes direct host/container HTTP and the in-process
        # monitor scheduler. No DB schema changes occur in an OS-only refresh.
        for project in PROJECTS: run(['docker', 'stop', '--time', '60', identity(p, project)[0]])
        idle(p)
        for project in PROJECTS: apply_image(p, record, project, new[project], directory, 'candidate')
        record['status'] = 'committed'; save(journal, record)
    except BaseException:
        recover(p, journal)
        raise
    finally:
        if pid:
            import signal
            try: os.kill(pid, signal.SIGCONT)
            except ProcessLookupError: pass
        if watchdog: watchdog.stdin.close(); watchdog.wait(timeout=5)
        if record['status'] == 'committed' or json.loads(journal.read_text())['status'] == 'rolled-back':
            if gate.exists(): release_gate(p, journal)
    status_report({'status': 'updated', 'checks': record['checks'], 'transaction': str(journal)})


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--recover', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--promote-prepared', type=Path)
    args = parser.parse_args(); ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid() or ROOT.stat().st_mode & 0o077:
        raise RuntimeError('unsafe state directory')
    with open(ROOT / 'owner.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.recover: return
            raise RuntimeError('another owner active; update deferred')
        p = helper()
        # Also serialize with readers, prior updater and other deployment owners.
        with open(p.UPDATER / 'run.lock', 'a') as first, open(p.UPDATER / 'update.lock', 'a') as second:
            try:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if args.recover: return
                raise RuntimeError('deployment lock busy; update deferred')
            for journal in sorted(ROOT.glob('run-*/transaction.json')):
                recover(p, journal)
            if args.promote_prepared:
                path = args.promote_prepared.resolve(strict=True)
                if path.parent.parent != ROOT or not path.parent.name.startswith('run-') or path.name != 'transaction.json':
                    raise RuntimeError('invalid prepared journal path')
                promote(p, path)
            elif not args.recover: update(p, ROOT, args.prepare_only)


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        if ROOT.exists(): status_report({'status': 'error', 'error': str(exc)})
        print('security refresh failed: ' + str(exc), file=sys.stderr); sys.exit(1)
