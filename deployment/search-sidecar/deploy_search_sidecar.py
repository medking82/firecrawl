#!/usr/bin/env python3
"""Switch GA401 search between the in-core preload and the isolated sidecar.

Run as root from a staged copy of deployment/search-sidecar. Holds the updater
run/update locks, so the core updater and security refresh cannot interleave.
apply:    preload -> sidecar. Backs up the exact override, worker file and live
          image IDs, recreates only api and search-worker, then verifies the
          core loads no search code. Any failure restores the backup.
          From an earlier sidecar worker release it replaces only the worker
          file and recreates only search-worker.
rollback: restores a backup made by apply (default: the newest one).
status:   prints the active profile and the live search runtime facts.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

HERE = Path(__file__).resolve().parent
REPO = Path('/home/marck/github/firecrawl')
OVERRIDE = REPO / 'docker-compose.override.yaml'
WORKER = Path('/opt/firecrawl-search/search-worker.cjs')
STATE = Path('/home/marck/.local/state/firecrawl-updater')
BACKUPS = STATE / 'search-sidecar'
SERVICES = ('search-worker', 'api')
CONTAINERS = {'api': 'firecrawl-api-1', 'search-worker': 'firecrawl-search-worker-1'}
PRELOAD_WORKER = '346ae3e267ecaa43cf5bbcfd6b3df12a33ba7d21f603edcaba42128526d33b53'
DDG_BLOCK = 'html.duckduckgo.com:127.0.0.1'
# Reviewed earlier sidecar worker releases that apply may upgrade and rollback may restore.
PREVIOUS_SIDECAR_WORKERS = {
    '5d8b40a5cc067fed631015b78224cdf7068c6c261ee0482826c02f0086b101f1': 'sidecar-01286ab',
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(*args, timeout=300):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True, timeout=timeout).stdout


def inspect(name):
    return json.loads(run('docker', 'inspect', name, timeout=30))[0]


def expected():
    sidecar = sha(HERE / 'docker-compose.override.sidecar.yaml')
    known = {'sidecar': {'override': sidecar, 'worker': sha(HERE / 'runtime/search-worker.cjs')},
             'preload': {'override': sha(HERE / 'docker-compose.override.preload.yaml'),
                         'worker': PRELOAD_WORKER}}
    for worker, name in PREVIOUS_SIDECAR_WORKERS.items():
        known[name] = {'override': sidecar, 'worker': worker}
    return known


def sidecar_like(name):
    return name == 'sidecar' or name in PREVIOUS_SIDECAR_WORKERS.values()


def profile():
    current = {'override': sha(OVERRIDE), 'worker': sha(WORKER)}
    for name, value in expected().items():
        if current == value:
            return name
    raise SystemExit('unknown search profile on host; refusing to change it')


@contextlib.contextmanager
def maintenance():
    handles = []
    try:
        for name in ('run.lock', 'update.lock'):
            handle = (STATE / name).open('a')
            handles.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise SystemExit('updater lock busy; retry later') from None
    finally:
        for handle in reversed(handles):
            handle.close()


def place(source, dest):
    # Same inode metadata as the replaced file; atomic rename on the same filesystem.
    info = dest.stat()
    tmp = dest.with_name('.' + dest.name + '.search-sidecar.tmp')
    shutil.copyfile(source, tmp)
    os.chown(tmp, info.st_uid, info.st_gid)
    os.chmod(tmp, info.st_mode & 0o7777)
    os.replace(tmp, dest)


def compose_files():
    labels = inspect(CONTAINERS['api'])['Config']['Labels']
    files = labels['com.docker.compose.project.config_files'].split(',')
    if labels.get('com.docker.compose.project') != 'firecrawl' or files[:2] != [
            str(REPO / 'docker-compose.yaml'), str(OVERRIDE)]:
        raise SystemExit('unexpected compose project for api')
    for extra in files[2:]:
        path = Path(extra)
        if not path.is_file() or path.is_symlink() or not str(path).startswith(str(STATE) + '/'):
            raise SystemExit('api compose overlay missing or outside updater state: ' + extra)
    return files


def recreate(files, services=SERVICES):
    args = ['sudo', '-u', 'marck', 'docker', 'compose', '--project-directory', REPO]
    for item in files:
        args += ['-f', item]
    run(*args, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', *services, timeout=600)


def wait_ready(deadline=180):
    end = time.time() + deadline
    while time.time() < end:
        state = inspect(CONTAINERS['api'])['State']
        if state['Running']:
            try:
                run('curl', '-fsS', '-o', '/dev/null', '--max-time', '5', 'http://127.0.0.1:3002/', timeout=10)
                return
            except subprocess.CalledProcessError:
                pass
        time.sleep(3)
    raise SystemExit('api not ready after recreate')


def live(images=None):
    api, worker = inspect(CONTAINERS['api']), inspect(CONTAINERS['search-worker'])
    env = dict(v.split('=', 1) for v in api['Config']['Env'] if '=' in v)
    facts = {
        'api_image': api['Image'], 'worker_image': worker['Image'],
        'api_search_env': {k: v for k, v in env.items() if k.startswith(('SEARCH_', 'SEARXNG_')) or k == 'NODE_OPTIONS'},
        'api_search_mounts': sorted(m['Destination'] for m in api['Mounts'] if m['Destination'].startswith('/app/')),
        'api_ddg_blocked': DDG_BLOCK in (api['HostConfig'].get('ExtraHosts') or []),
        'running': api['State']['Running'] and worker['State']['Running'],
    }
    if images and (facts['api_image'], facts['worker_image']) != (images['api'], images['search-worker']):
        raise SystemExit('image changed during search profile switch')
    return facts


def verify(name, images):
    facts = live(images)
    ok = facts['running'] and (
        (sidecar_like(name) and facts['api_ddg_blocked'] and not facts['api_search_mounts']
         and facts['api_search_env'].get('SEARXNG_ENDPOINT') == 'http://search-worker:8081'
         and 'NODE_OPTIONS' not in facts['api_search_env'])
        or (name == 'preload' and not facts['api_ddg_blocked']
            and facts['api_search_env'].get('NODE_OPTIONS') == '--require=/app/searxng-timeout.cjs'))
    if not ok:
        raise SystemExit('live search runtime does not match ' + name + ': ' + json.dumps(facts))
    return facts


def switch(override, worker, name, images, files, services=SERVICES):
    place(override, OVERRIDE)
    place(worker, WORKER)
    recreate(files, services)
    wait_ready()
    if profile() != name:
        raise SystemExit('host files do not match ' + name)
    return verify(name, images)


def apply():
    with maintenance():
        current = profile()
        if current == 'sidecar':
            print(json.dumps({'status': 'already-sidecar', 'live': verify('sidecar', None)}))
            return
        files = compose_files()
        images = {k: inspect(v)['Image'] for k, v in CONTAINERS.items()}
        backup = BACKUPS / time.strftime('%Y%m%d-%H%M%S')
        backup.mkdir(mode=0o700, parents=True)
        shutil.copy2(OVERRIDE, backup / 'docker-compose.override.yaml')
        shutil.copy2(WORKER, backup / 'search-worker.cjs')
        (backup / 'live.json').write_text(json.dumps({'images': images, 'compose_files': files, 'profile': current}))
        for path in (BACKUPS, backup, *backup.iterdir()):
            os.chown(path, 1000, 1000)
        # The override is the same for every sidecar release: only the worker restarts.
        services = ('search-worker',) if sidecar_like(current) else SERVICES
        try:
            facts = switch(HERE / 'docker-compose.override.sidecar.yaml', HERE / 'runtime/search-worker.cjs',
                           'sidecar', images, files, services)
        except BaseException as error:
            switch(backup / 'docker-compose.override.yaml', backup / 'search-worker.cjs', current, images, files,
                   services)
            raise SystemExit('sidecar apply failed and was rolled back: ' + str(error)) from None
        print(json.dumps({'status': 'sidecar', 'backup': str(backup), 'live': facts}))


def rollback(path):
    with maintenance():
        backup = path or max(p for p in BACKUPS.iterdir() if p.is_dir())
        record = json.loads((backup / 'live.json').read_text())
        target = record.get('profile')
        state = {'override': sha(backup / 'docker-compose.override.yaml'), 'worker': sha(backup / 'search-worker.cjs')}
        if target == 'sidecar' or expected().get(target) != state:
            raise SystemExit('backup is not a reviewed earlier search state')
        if profile() != 'sidecar':
            raise SystemExit('host is not on the current sidecar release')
        services = ('search-worker',) if sidecar_like(target) else SERVICES
        facts = switch(backup / 'docker-compose.override.yaml', backup / 'search-worker.cjs', target,
                       record['images'], compose_files(), services)
        print(json.dumps({'status': target, 'backup': str(backup), 'live': facts}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('apply', 'rollback', 'status'))
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('run as root')
    if args.action == 'apply':
        apply()
    elif args.action == 'rollback':
        rollback(args.backup)
    else:
        print(json.dumps({'profile': profile(), 'live': live()}))


if __name__ == '__main__':
    main()
