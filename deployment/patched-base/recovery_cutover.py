"""Reviewed GA401 recovery cutover. Never run until the frozen change is reviewed.

Uses existing Compose references; does not build, pull, remove volumes or roll back.
"""
import argparse
import contextlib
import hashlib
import http.client
import json
import os
import signal
import stat
import sys
import time
import uuid
from pathlib import Path

import promote as p
from recovery_backup import verify_backup
from recovery_volume import postgres_volume, verify_same_volume

DB = {'firecrawl': 'nuq-postgres', 'firecrawl-monitor': 'app-db', 'firecrawl-browser': 'app-db'}
ENV = {'firecrawl-monitor': p.MONITOR / 'runtime.env', 'firecrawl-browser': p.BROWSER / 'runtime.env'}
APP_DB = {'firecrawl-monitor': 'monitor', 'firecrawl-browser': 'browser'}

# Compare the actual executables, extensions, SQL, and browser/application trees.
# No host mounts or network are provided to the disposable recovery image.
PG_PATHS = '/usr/lib/postgresql /usr/share/postgresql /docker-entrypoint-initdb.d /usr/local/bin/docker-entrypoint.sh'
PW_PATHS = '/usr/src/app /usr/local/share/playwright /usr/local/bin/node'
FINGERPRINT = ("set -eu; for p in %s; do test -e \"$p\"; done; "
               "find %s -type f %s -exec sha256sum {} +; "
               "printf '__LINKS__\\n'; find %s -type l -printf '%%p -> %%l\\n'")


@contextlib.contextmanager
def locks():
    if p.fcntl is None or not hasattr(os, 'O_NOFOLLOW'):
        raise p.Stop('Linux secure flock required')
    with contextlib.ExitStack() as stack:
        for path in (p.UPDATER / 'run.lock', p.UPDATER / 'update.lock', p.BROWSER / 'manager-activation.lock'):
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stream = stack.enter_context(os.fdopen(fd, 'r+'))
            info = os.fstat(fd)
            actual = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)):
                raise p.Stop('unsafe coordination lock: ' + path.name)
            try:
                p.fcntl.flock(fd, p.fcntl.LOCK_EX | p.fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise p.Stop('coordination lock busy: ' + path.name) from exc
        yield


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(container, image, paths):
    volumes = p.inspect('image', image).get('Config', {}).get('Volumes') or {}
    if set(volumes) - {'/var/lib/postgresql/data'}:
        raise p.Stop('recovery image declares unexpected volumes')
    is_playwright = paths == PW_PATHS
    exclusion = "! -path /usr/src/app/node_modules/.pnpm-workspace-state-v1.json" if is_playwright else ''
    user = '1000' if is_playwright else '0'
    command = FINGERPRINT % (paths, paths, exclusion, paths)
    current_cmd = ['docker', 'exec', '-u', user, container]
    candidate_cmd = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                       '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                       '--tmpfs', '/var/lib/postgresql/data:rw,noexec,nosuid,size=16m',
                       '--user', user, '--entrypoint', 'sh', image]
    current = p.run(current_cmd + ['sh', '-c', command], timeout=300)
    candidate = p.run(candidate_cmd + ['-c', command], timeout=300)
    def normalized(output):
        parts = output.split('__LINKS__')
        if len(parts) != 2: raise p.Stop('incomplete image fingerprint')
        return [sorted(part.splitlines()) for part in parts]
    if normalized(current) != normalized(candidate):
        raise p.Stop('recovery image executable or SQL fingerprint differs')
    if is_playwright:
        file = '/usr/src/app/node_modules/.pnpm-workspace-state-v1.json'
        a = json.loads(p.run(current_cmd + ['cat', file], timeout=30))
        b = json.loads(p.run(candidate_cmd + ['-c', 'cat '+file], timeout=30))
        a.pop('lastValidatedTimestamp', None); b.pop('lastValidatedTimestamp', None)
        if a != b: raise p.Stop('Playwright workspace state differs')
    return hashlib.sha256(json.dumps(normalized(current),separators=(',',':')).encode()).hexdigest()


def sql(container, db, query):
    return p.run(['docker', 'exec', container, 'psql', '-X', '-U', 'postgres', '-d', db,
                  '-v', 'ON_ERROR_STOP=1', '-At', '-c', query], timeout=30)


def queues_empty(project, container):
    queue = sql(container, 'postgres', "SELECT (SELECT count(*) FROM nuq.queue_scrape WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_crawl_finished WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_scrape_backlog)")
    if queue != '0':
        return False
    if project == 'firecrawl-monitor':
        if sql(container, 'monitor', "SELECT count(*) FROM public.monitor_checks WHERE status IN ('queued','running')") != '0':
            return False
    if project == 'firecrawl-browser' and p.active_sessions():
        return False
    return True


def idle(project, container):
    activity = sql(container, 'postgres', "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state = 'active'")
    return activity == '0' and queues_empty(project, container)


def wait_for_drain(before, seconds=120):
    deadline = time.monotonic() + seconds
    while True:
        if (not p.active_sessions() and all(queues_empty(name, before[name]['services'][DB[name]]['Id'])
                                             for name in p.PROJECTS)):
            return
        if time.monotonic() >= deadline:
            raise p.Stop('queues or browser sessions did not drain')
        time.sleep(2)


def db_ready(project, container):
    deadline = time.monotonic() + 120
    while True:
        try:
            p.run(['docker', 'exec', container, 'pg_isready', '-U', 'postgres', '-d', 'postgres'], timeout=10)
            break
        except p.Stop:
            if time.monotonic() >= deadline:
                raise p.Stop('postgres readiness timeout: ' + project)
            time.sleep(2)
    if sql(container, 'postgres', "SELECT count(*) FROM pg_extension WHERE extname='pg_cron'") != '1':
        raise p.Stop('pg_cron extension missing: ' + project)
    if 'pg_cron' not in [item.strip() for item in sql(container, 'postgres', 'SHOW shared_preload_libraries').split(',')]:
        raise p.Stop('pg_cron is not preloaded: ' + project)
    sql(container, 'postgres', 'SELECT count(*) FROM cron.job')
    sql(container, 'postgres', 'SELECT count(*) FROM nuq.queue_scrape')
    sql(container, 'postgres', 'SELECT count(*) FROM nuq.queue_crawl_finished')
    sql(container, 'postgres', 'SELECT count(*) FROM nuq.queue_scrape_backlog')
    if project == 'firecrawl-monitor':
        sql(container, 'monitor', 'SELECT count(*) FROM public.monitor_checks')
        sql(container, 'monitor', 'SELECT count(*) FROM public.monitors')
    if project == 'firecrawl-browser':
        sql(container, 'browser', 'SELECT count(*) FROM public.browser_sessions')


def wait_api(project):
    deadline = time.monotonic() + 180
    while True:
        try:
            if p.readiness(p.PORTS[project]):
                return
        except (OSError, http.client.HTTPException, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise p.Stop('API readiness timeout: ' + project)
        time.sleep(2)


def file_hashes(snapshot, envfile):
    files = list(snapshot['paths']) + ([str(envfile)] if envfile else [])
    if snapshot['project'] == 'firecrawl' and not envfile:
        files.append(str(Path(snapshot['workdir']) / '.env'))
    return {file: digest_file(file) for file in files}

def stable_summary(snapshot):
    return {name: {'id':row['id'], 'image':row['image'],
                   'running':row['state']['Running'], 'started':row['state']['StartedAt'],
                   'mounts':sorted(row['mounts'],key=lambda item:item['Destination'])}
            for name,row in p.summary(snapshot).items()}


def same_inputs(snapshot, envfile, hashes, config_hash):
    if file_hashes(snapshot, envfile) != hashes:
        raise p.Stop('Compose file or private environment changed')
    encoded = json.dumps(p.render(snapshot, envfile=envfile), sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(encoded).hexdigest() != config_hash:
        raise p.Stop('effective Compose configuration changed')


def cutover(backup, max_age):
    os.umask(0o077)
    with locks():
        backup_receipt = verify_backup(backup, max_age)
        before = {name: p.project_snapshot(name) for name in p.PROJECTS}
        evidence_dir = p.UPDATER / ('recovery-cutover-' + uuid.uuid4().hex)
        evidence_dir.mkdir(mode=0o700)
        record = {'status': 'preparing', 'backup': backup_receipt, 'projects': {}}
        def save():
            p.atomic_write(evidence_dir / 'receipt.json', json.dumps(record, indent=2).encode())
        save()
        try:
            for name in p.PROJECTS:
                snap = before[name]
                envfile = ENV.get(name) or snap.get('envfile')
                if name in ENV and snap.get('envfile') and Path(snap['envfile']) != envfile:
                    raise p.Stop('unexpected private env file: ' + name)
                config = p.render(snap, envfile=envfile)
                hashes = file_hashes(snap, envfile)
                config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                targets = [DB[name]] + (['playwright-service'] if name == 'firecrawl' else [])
                images = {}
                for service in targets:
                    row = snap['services'][service]
                    if not row['State']['Running']:
                        raise p.Stop('target service is not running: ' + name + '/' + service)
                    ref = config['services'][service].get('image')
                    if not ref:
                        raise p.Stop('persistent Compose image missing')
                    image = p.require_id(p.inspect('image', ref)['Id'])
                    if ref != image:
                        raise p.Stop('persistent recovery image must be an immutable ID')
                    images[service] = image
                    fingerprint(row['Id'], image, PW_PATHS if service == 'playwright-service' else PG_PATHS)
                record['projects'][name] = {'before': p.summary(snap), 'file_sha256': hashes,
                                            'config_sha256': config_hash, 'target_images': images,
                                            'volume': postgres_volume(snap['services'][DB[name]])}
                save()
            wait_for_drain(before)
            gate = p.BROWSER / 'admission-paused'
            pid = p.manager_pid(); watchdog = None; frozen = False; success = False
            try:
                with gate.open('x') as stream:
                    stream.write(evidence_dir.name)
                record['status'] = 'admission-paused'; save()
                watchdog = p.start_thaw_watchdog(pid)
                frozen = True; p.freeze(pid)
                if p.manager_pid() != pid or p.active_sessions():
                    raise p.Stop('browser manager or sessions changed')
                for name in p.PROJECTS:
                    snap = before[name]; entry = record['projects'][name]
                    envfile = ENV.get(name) or snap.get('envfile')
                    same_inputs(snap, envfile, entry['file_sha256'], entry['config_sha256'])
                    if stable_summary(p.project_snapshot(name)) != stable_summary(snap):
                        raise p.Stop('container state changed: ' + name)
                    api = snap['services']['api']['Id']
                    database = snap['services'][DB[name]]['Id']
                    record['status'] = 'stopping-api-' + name; save()
                    p.run(['docker', 'stop', '--time', '60', api], timeout=90)
                    if p.inspect('container', api)['State']['Running']:
                        raise p.Stop('API did not stop: ' + name)
                    record['status'] = 'checking-idle-' + name; save()
                    try:
                        is_idle = idle(name, database) and not p.active_sessions()
                    except Exception:
                        is_idle = False
                    if not is_idle:
                        # Restore the exact existing API, never recreate it or roll back data.
                        p.run(['docker', 'start', api], timeout=90)
                        if p.inspect('container', api)['Image'] != snap['services']['api']['Image']:
                            raise p.Stop('restored API image changed: ' + name)
                        wait_api(name)
                        raise p.Stop('busy or unverified database; original API restarted: ' + name)
                    if gate.read_text() != evidence_dir.name or p.manager_pid() != pid:
                        raise p.Stop('admission gate or manager changed')
                    if p.inspect('container', api)['State']['Running']:
                        raise p.Stop('API restarted during idle check')
                    current_db = p.inspect('container', database)
                    if not current_db['State']['Running'] or postgres_volume(current_db) != entry['volume']:
                        raise p.Stop('database or volume changed before recreate')
                    same_inputs(snap, envfile, entry['file_sha256'], entry['config_sha256'])
                    for service, image in entry['target_images'].items():
                        ref = p.render(snap, envfile=envfile)['services'][service]['image']
                        if p.inspect('image', ref)['Id'] != image:
                            raise p.Stop('image reference changed before recreate')
                    record['status'] = 'recreating-db-' + name; save()
                    p.compose(name, snap['paths'], snap['workdir'], envfile, 'up', '-d', '--no-deps',
                              '--no-build', '--pull', 'never', '--force-recreate', DB[name])
                    ids = p.run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + name,
                                 '--filter', 'label=com.docker.compose.service=' + DB[name]]).splitlines()
                    if len(ids) != 1:
                        raise p.Stop('unexpected recreated database count')
                    new_db = p.inspect('container', ids[0])
                    if new_db['Image'] != entry['target_images'][DB[name]]:
                        raise p.Stop('recreated database image mismatch')
                    verify_same_volume(snap['services'][DB[name]], new_db)
                    entry['after_db_id'] = new_db['Id']; save()
                    db_ready(name, new_db['Id'])
                    if name == 'firecrawl':
                        record['status'] = 'recreating-playwright'; save()
                        p.compose(name, snap['paths'], snap['workdir'], envfile, 'up', '-d', '--no-deps',
                                  '--no-build', '--pull', 'never', '--force-recreate', 'playwright-service')
                        ids = p.run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + name,
                                     '--filter', 'label=com.docker.compose.service=playwright-service']).splitlines()
                        if len(ids) != 1:
                            raise p.Stop('unexpected playwright container count')
                        playwright = p.inspect('container', ids[0])
                        if (playwright['Image'] != entry['target_images']['playwright-service']
                                or not playwright['State']['Running']):
                            raise p.Stop('recreated playwright image mismatch')
                        entry['after_playwright_id'] = ids[0]; save()
                    record['status'] = 'starting-api-' + name; save()
                    p.run(['docker', 'start', api], timeout=90)
                    if p.inspect('container', api)['Image'] != snap['services']['api']['Image']:
                        raise p.Stop('API image changed')
                    wait_api(name)
                    record['status'] = 'ready-' + name; save()
                if p.manager_pid() != pid or p.active_sessions():
                    raise p.Stop('browser manager or sessions changed')
                success = True
            finally:
                if frozen:
                    os.kill(pid, signal.SIGCONT)
                if watchdog:
                    watchdog.stdin.close(); watchdog.wait(timeout=5)
                if success:
                    if gate.read_text() != evidence_dir.name:
                        raise p.Stop('admission gate ownership changed')
                    gate.unlink()
            record['status'] = 'complete'; save()
            return evidence_dir / 'receipt.json'
        except Exception as exc:
            record['failure_stage'] = record['status']
            record['failure'] = str(exc) if isinstance(exc, p.Stop) else type(exc).__name__
            record['status'] = 'failed-manual-intervention'; save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--max-backup-age-seconds', required=True, type=int)
    args = parser.parse_args()
    print(json.dumps({'status': 'complete', 'receipt': str(cutover(args.backup, args.max_backup_age_seconds))}))


if __name__ == '__main__':
    def interrupted(_signum, _frame):
        raise p.Stop('cutover interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except Exception as exc:
        print('cutover stopped: ' + (str(exc) if isinstance(exc, p.Stop) else type(exc).__name__) +
              '; preserve evidence; no automatic data rollback', file=sys.stderr)
        sys.exit(1)
