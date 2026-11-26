"""Switch an installed Browser or Monitor extension to a reviewed release, or roll it back.

The current runtime.env, the user units the installer rewrites and the running Compose
definition are saved before the switch. When the target release fails to install or to
answer readiness and an authenticated list request, that snapshot is restored and checked.
Storage images (PostgreSQL, Redis, RabbitMQ) are never changed here.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extension_release import IMAGE_KEYS, STORAGE_KEYS, load_release, lock_installation


UNITS = {
    'browser': ('firecrawl-browser-manager.service', 'firecrawl-browser-llm.service'),
    'monitor': ('firecrawl-monitor-judge.service', 'firecrawl-monitor-prune.service',
                'firecrawl-monitor-prune.timer'),
}
PORT = {'browser': 3007, 'monitor': 3006}
API_KEY = {'browser': 'BROWSER_API_IMAGE', 'monitor': 'MONITOR_IMAGE'}
SECURITY_REFRESH = Path('.local/state/firecrawl-security-refresh/owner.lock')
IGNORED_DIRECTIVES = {'Description', 'ExecStart'}


class Extension:
    def __init__(self, service, home=None):
        self.service = service
        self.home = Path(home or Path.home())
        self.state = self.home / f'services/firecrawl-{service}/shared'
        self.envfile = self.state / 'runtime.env'
        self.units = self.home / '.config/systemd/user'
        self.project = 'firecrawl-' + service

    def env(self):
        return dict(line.split('=', 1) for line in self.envfile.read_text().splitlines() if '=' in line)

    def containers(self):
        ids = subprocess.check_output(
            ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + self.project],
            text=True, timeout=20).split()
        if not ids:
            return []
        return json.loads(subprocess.check_output(['docker', 'container', 'inspect', *ids],
                                                  text=True, timeout=20))

    def api_image(self):
        for row in self.containers():
            if row['Config']['Labels'].get('com.docker.compose.service') == 'api' and row['State']['Running']:
                return row['Image']
        return None

    def healthy(self, timeout=90):
        port = PORT[self.service]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/v0/health/readiness', timeout=3) as response:
                    if json.load(response).get('status') == 'ok':
                        break
            except Exception:
                pass
            time.sleep(2)
        else:
            return 'readiness timeout'
        client = json.loads((self.state / 'client.json').read_text())
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v2/{self.service}',
                                         headers={'Authorization': 'Bearer ' + client['api_key']})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.load(response)
        except Exception as error:
            return 'authenticated list failed: ' + type(error).__name__
        field = 'sessions' if self.service == 'browser' else 'data'
        if body.get('success') is not True or not isinstance(body.get(field), list):
            return 'authenticated list returned an unexpected body'
        return None


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check, text=True,
                          capture_output=True, timeout=60)


def private_copy(source, target):
    shutil.copyfile(source, target)
    target.chmod(0o600)


def unit_settings(path):
    settings = set()
    for source in [path, *sorted(Path(str(path) + '.d').glob('*.conf'))]:
        if not source.is_file():
            continue
        for line in source.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith(('#', ';', '[')) or '=' not in line:
                continue
            key, value = (part.strip() for part in line.split('=', 1))
            if key in IGNORED_DIRECTIVES:
                continue
            if key == 'Environment':
                settings.update('Environment ' + pair.split('=', 1)[0] for pair in value.split())
            else:
                settings.add(f'{key}={value}')
    return settings


def lost_unit_settings(ext, snapshot):
    lost = []
    for name, unit in snapshot['units'].items():
        missing = set(unit['settings']) - unit_settings(ext.units / name)
        lost += [f'{name}: {setting}' for setting in sorted(missing)]
    return lost


def lock_security_refresh(ext):
    path = ext.home / SECURITY_REFRESH
    if not path.parent.is_dir():
        return None
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise RuntimeError('a Firecrawl security refresh is running; retry after it finishes')
    return descriptor


def take_snapshot(ext, directory):
    directory.mkdir(parents=True, mode=0o700)
    private_copy(ext.envfile, directory / 'runtime.env')
    rows = ext.containers()
    api = next((row for row in rows if row['Config']['Labels'].get('com.docker.compose.service') == 'api'), None)
    if api is None:
        raise RuntimeError('no installed API container to snapshot')
    labels = api['Config']['Labels']
    units = {}
    for name in UNITS[ext.service]:
        path = ext.units / name
        if path.is_file():
            private_copy(path, directory / name)
        active = systemctl('is-active', name, check=False).stdout.strip() == 'active'
        units[name] = {'present': path.is_file(), 'active': active, 'settings': sorted(unit_settings(path))}
    snapshot = {
        'service': ext.service,
        'api_image': api['Image'],
        'compose_files': labels['com.docker.compose.project.config_files'].split(','),
        'compose_services': sorted({row['Config']['Labels'].get('com.docker.compose.service') for row in rows}),
        'units': units,
        'storage': {key: value for key, value in ext.env().items() if key in STORAGE_KEYS},
    }
    for path in snapshot['compose_files']:
        if not Path(path).is_file():
            raise RuntimeError('running Compose definition is missing: ' + path)
    (directory / 'snapshot.json').write_text(json.dumps(snapshot, indent=2))
    return snapshot


def restore(ext, directory):
    snapshot = json.loads((directory / 'snapshot.json').read_text())
    if snapshot['service'] != ext.service:
        raise RuntimeError('snapshot belongs to another extension')
    if {key: value for key, value in ext.env().items() if key in STORAGE_KEYS} != snapshot['storage']:
        raise RuntimeError('storage images changed since the snapshot; use the extension upgrade owner')
    problems = []

    def attempt(label, *args, **kwargs):
        try:
            subprocess.run(*args, check=True, capture_output=True, text=True, **kwargs)
        except (OSError, subprocess.SubprocessError) as error:
            problems.append(f'{label}: {type(error).__name__}')

    lock = lock_installation(ext.state)
    try:
        temp = ext.envfile.with_suffix('.restore')
        private_copy(directory / 'runtime.env', temp)
        temp.replace(ext.envfile)
        for name, unit in snapshot['units'].items():
            target = ext.units / name
            if unit['present']:
                shutil.copyfile(directory / name, target)
            elif target.exists():
                attempt('disable ' + name, ['systemctl', '--user', 'disable', '--now', name], timeout=60)
                target.unlink()
        attempt('daemon-reload', ['systemctl', '--user', 'daemon-reload'], timeout=60)
        for name, unit in snapshot['units'].items():
            if unit['active']:
                attempt('restart ' + name, ['systemctl', '--user', 'restart', name], timeout=60)
            elif unit['present']:
                attempt('stop ' + name, ['systemctl', '--user', 'stop', name], timeout=60)
        saved = dict(line.split('=', 1) for line in (directory / 'runtime.env').read_text().splitlines() if '=' in line)
        environment = {key: value for key, value in os.environ.items() if key not in saved}
        command = ['docker', 'compose', '--project-name', ext.project, '--env-file', str(ext.envfile)]
        for path in snapshot['compose_files']:
            command += ['-f', path]
        attempt('compose up', [*command, 'up', '--no-build', '--pull', 'never', '-d', *snapshot['compose_services']],
                env=environment, timeout=300)
    finally:
        os.close(lock)
    problem = ext.healthy()
    if problem is None and ext.api_image() != snapshot['api_image']:
        problem = 'API image differs from the snapshot after restore'
    return '; '.join(problems + ([problem] if problem else [])) or None


def switch(ext, release_dir, directory, replace_refreshed=False):
    script = release_dir / f'deployment/self-hosted-{ext.service}/deploy.py'
    manifest = release_dir / 'release-images.json'
    inputs = json.loads(subprocess.check_output([sys.executable, '-B', str(script), '--release-inputs'],
                                                text=True, timeout=30))
    release = load_release(manifest, release_dir, ext.service, inputs['files'])
    current = ext.env()
    changed = {key for key in IMAGE_KEYS[ext.service] if current.get(key) != release['images'][key]}
    if changed & STORAGE_KEYS:
        raise RuntimeError('target release changes storage images; use the extension upgrade owner')
    live = ext.api_image()
    target = release['images'][API_KEY[ext.service]]
    if live and live != current.get(API_KEY[ext.service]) and live != target and not replace_refreshed:
        raise RuntimeError('the running API image came from a security refresh and the target release does not '
                           'contain it; build the release from that image or pass --replace-refreshed-image')
    snapshot = take_snapshot(ext, directory)
    environment = {**os.environ, 'HOME': str(ext.home)}
    with (directory / 'install.log').open('w') as log:
        result = subprocess.run([sys.executable, '-B', str(script), '--release-images', str(manifest), '--switch'],
                                stdout=log, stderr=log, env=environment, timeout=600)
    problem = 'installer exited with status %d' % result.returncode if result.returncode else ext.healthy()
    if problem is None and ext.api_image() != target:
        problem = 'running API image differs from the target release'
    if problem is None and (lost := lost_unit_settings(ext, snapshot)):
        problem = 'unit settings would be lost (move operator settings to a drop-in): ' + ', '.join(lost)
    receipt = {'service': ext.service, 'target': str(release_dir), 'target_manifest': release['manifest_sha256'],
               'previous_api_image': snapshot['api_image'], 'changed': sorted(changed), 'snapshot': str(directory)}
    if problem is None:
        receipt['status'] = 'switched'
        return receipt, 0
    receipt['failure'] = problem
    try:
        rollback_problem = restore(ext, directory)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        rollback_problem = f'{type(error).__name__}: {error}'
    if rollback_problem is None:
        receipt['status'] = 'rolled_back'
        return receipt, 2
    receipt.update(status='rollback_failed', rollback_failure=rollback_problem)
    return receipt, 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('service', choices=sorted(UNITS))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--release', type=Path, help='reviewed release directory to switch to')
    action.add_argument('--rollback', type=Path, help='snapshot directory written by an earlier switch')
    action.add_argument('--check', action='store_true', help='only check readiness and the authenticated list')
    parser.add_argument('--replace-refreshed-image', action='store_true',
                        help='allow replacing an API image installed by the security refresh')
    args = parser.parse_args(argv)
    ext = Extension(args.service)
    if args.check:
        problem = ext.healthy(timeout=10)
        print(json.dumps({'service': ext.service, 'api_image': ext.api_image(), 'healthy': problem is None,
                          **({'failure': problem} if problem else {})}))
        return 0 if problem is None else 1
    switches = ext.state / 'switches'
    switches.mkdir(mode=0o700, exist_ok=True)
    lock = os.open(switches / 'switch.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    refresh_lock = lock_security_refresh(ext)
    if args.rollback:
        directory = args.rollback.resolve()
        if directory.parent != switches.resolve():
            parser.error('rollback snapshots must come from ' + str(switches))
        problem = restore(ext, directory)
        receipt, code = ({'service': ext.service, 'status': 'restored', 'snapshot': str(directory)}, 0)
        if problem:
            receipt, code = ({**receipt, 'status': 'rollback_failed', 'rollback_failure': problem}, 3)
    else:
        directory = switches / time.strftime('%Y%m%dT%H%M%S', time.gmtime())
        receipt, code = switch(ext, args.release.resolve(), directory, args.replace_refreshed_image)
    (directory / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
