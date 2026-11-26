"""Verify the offline image lock and source files of a packaged extension."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


IMAGE_KEYS = {
    'browser': {'BROWSER_API_IMAGE', 'BROWSER_IMAGE', 'POSTGRES_IMAGE', 'REDIS_IMAGE', 'RABBIT_IMAGE'},
    'monitor': {'MONITOR_IMAGE', 'POSTGRES_IMAGE', 'REDIS_IMAGE', 'RABBIT_IMAGE'},
}


def load_release(manifest, source, service, required_files):
    raw = manifest.read_bytes()
    release = json.loads(raw)
    if (not isinstance(release, dict) or set(release) != {'service', 'source_commit', 'images', 'files'}
            or release['service'] != service or not isinstance(release['source_commit'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', release['source_commit'])):
        raise ValueError('invalid extension release identity')

    files = release['files']
    if not isinstance(files, dict) or set(files) != set(required_files):
        raise ValueError('extension release file manifest does not match the installer')
    for filename, expected in files.items():
        path = source / filename
        if (not path.is_file() or path.resolve() != path
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('extension release file mismatch: ' + filename)

    images = release['images']
    if (not isinstance(images, dict) or set(images) != IMAGE_KEYS[service]
            or any(not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
                   for image in images.values())):
        raise ValueError('extension images must be exact local Docker image IDs')
    for name, image in sorted(images.items()):
        result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                                capture_output=True, text=True, timeout=10)
        if result.returncode or result.stdout.strip() != image:
            raise ValueError('reviewed extension image unavailable: ' + name)
    return {**release, 'manifest_sha256': hashlib.sha256(raw).hexdigest()}


def select_release(source, envfile, service, filenames):
    directory = 'deployment/self-hosted-' + service
    required_files = (['deployment/extension_release.py', 'nuq.sql']
                      + [f'{directory}/{name}' for name in filenames])
    if service == 'browser':
        required_files += ['deployment/self-hosted-monitor/001-monitor.sql',
                           'deployment/self-hosted-monitor/magpie_judge.py']
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-images', type=Path, help='reviewed image and source manifest')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check-images', action='store_true', help='verify a release without installing it')
    modes.add_argument('--release-inputs', action='store_true', help='list the required manifest entries')
    args = parser.parse_args()
    if args.release_inputs:
        print(json.dumps({'service': service, 'image_keys': sorted(IMAGE_KEYS[service]),
                          'files': sorted(required_files)}))
        raise SystemExit(0)
    if args.release_images is None:
        if args.check_images or not envfile.is_file():
            parser.error('--release-images is required for first installation or --check-images')
        return None
    try:
        release = load_release(args.release_images, source, service, required_files)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    if args.check_images:
        print(json.dumps(release, sort_keys=True))
        raise SystemExit(0)
    return release


def install_images(release, config):
    if release is None:
        if 'BASE_API' not in config:
            raise ValueError('supply the installed extension release with --release-images')
        return config
    images = release['images']
    if config and any(config.get(name) != image for name, image in images.items()):
        raise ValueError('release images differ from the installed extension; use its upgrade owner')
    return {**config, **images}


def verify_project_images(release, project):
    api_key = 'BROWSER_API_IMAGE' if release['service'] == 'browser' else 'MONITOR_IMAGE'
    image_keys = {'api': api_key, 'app-db': 'POSTGRES_IMAGE',
                  'redis': 'REDIS_IMAGE', 'rabbitmq': 'RABBIT_IMAGE'}
    containers = subprocess.check_output(
        ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project],
        text=True, timeout=10).splitlines()
    if not containers:
        return
    rows = json.loads(subprocess.check_output(['docker', 'container', 'inspect', *containers],
                                              text=True, timeout=10))
    for row in rows:
        service = row['Config']['Labels'].get('com.docker.compose.service')
        key = image_keys.get(service)
        if key is None or row['Image'] != release['images'][key]:
            raise ValueError('running extension differs from the release; use its upgrade owner')


def lock_installation(state):
    lock = os.open(state / 'install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock)
        raise
    return lock


def ensure_app_database(compose, database):
    exists = compose('exec', '-T', 'app-db', 'psql', '-XAt', '-U', 'postgres', '-d', 'postgres',
                     '-v', 'ON_ERROR_STOP=1', '-c',
                     f"SELECT 1 FROM pg_database WHERE datname = '{database}'", capture_output=True)
    if not exists.stdout.strip():
        compose('exec', '-T', 'app-db', 'psql', '-X', '-U', 'postgres', '-d', 'postgres',
                '-v', 'ON_ERROR_STOP=1', '-c', f'CREATE DATABASE {database} TEMPLATE template0')
