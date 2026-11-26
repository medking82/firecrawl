import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from unittest import mock

from deployment import extension_switch
from deployment.extension_release import ensure_app_database, install_images, lock_installation


ROOT = Path(__file__).resolve().parents[1]


class ExtensionReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.source = self.home / 'package'
        self.source.mkdir()
        binary = self.home / 'docker'
        binary.write_text(f'#!{sys.executable}\n' + '''
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with (Path.home() / "docker.calls").open("a") as stream:
    print(json.dumps(args), file=stream)
if args[:2] == ["image", "inspect"]:
    if args[-1] == os.environ.get("MISSING_IMAGE"):
        sys.exit(1)
    print(args[-1])
elif args[0] == "ps":
    if os.environ.get("LIVE_IMAGE") and "-aq" in args:
        print("extension-api")
elif args[:2] == ["container", "inspect"]:
    print(json.dumps([{"Image": os.environ["LIVE_IMAGE"],
                        "Config": {"Labels": {"com.docker.compose.service": "api"}}}]))
elif args[0] == "compose":
    if any(name in os.environ for name in ("BROWSER_API_IMAGE", "MONITOR_IMAGE")):
        sys.exit(74)
    sys.exit(73)
else:
    sys.exit(75)
''')
        binary.chmod(0o700)
        self.env = {**os.environ, 'HOME': str(self.home),
                    'PATH': str(self.home) + os.pathsep + os.environ['PATH']}

    def deploy(self, service, *args, env=None):
        relative = Path(f'deployment/self-hosted-{service}/deploy.py')
        script = self.source / relative
        if not script.exists():
            return self.run_deploy(ROOT / relative, args, env)
        return self.run_deploy(script, args, env)

    def run_deploy(self, script, args, env):
        return subprocess.run([sys.executable, '-B', str(script), *args],
                              capture_output=True, text=True, timeout=10, env=env or self.env)

    def manifest(self, service):
        result = self.deploy(service, '--release-inputs')
        self.assertEqual(result.returncode, 0, result.stderr)
        inputs = json.loads(result.stdout)
        for path in inputs['files']:
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            origin = ROOT / ('apps/nuq-postgres/nuq.sql' if path == 'nuq.sql' else path)
            shutil.copyfile(origin, target)
        files = {path: hashlib.sha256((self.source / path).read_bytes()).hexdigest()
                 for path in inputs['files']}
        images = {key: f'sha256:{number:064x}'
                  for number, key in enumerate(inputs['image_keys'], 1)}
        self.assertNotIn('BASE_API', images)
        return {'service': service, 'source_commit': 'a' * 40, 'files': files, 'images': images}

    def write_manifest(self, release):
        path = self.home / 'release-images.json'
        path.write_text(json.dumps(release))
        return str(path)

    def calls(self):
        return [json.loads(line) for line in (self.home / 'docker.calls').read_text().splitlines()]

    def test_fresh_install_uses_only_release_images(self):
        for service, image_key in [('browser', 'BROWSER_API_IMAGE'), ('monitor', 'MONITOR_IMAGE')]:
            with self.subTest(service=service):
                release = self.manifest(service)
                path = self.write_manifest(release)
                result = self.deploy(service, '--release-images', path, '--check-images')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['images'], release['images'])
                self.assertFalse((self.home / 'services').exists() if service == 'browser'
                                 else (self.home / 'services/firecrawl-monitor').exists())
                (self.home / 'docker.calls').unlink()
                result = self.deploy(service, '--release-images', path,
                                     env={**self.env, image_key: 'wrong-image'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('exit status 73', result.stderr)
                calls = self.calls()
                self.assertEqual(calls[-1][-8:], ['up', '--no-build', '--pull', 'never',
                                                  '-d', 'app-db', 'redis', 'rabbitmq'])
                self.assertEqual({command[-1] for command in calls if command[:2] == ['image', 'inspect']},
                                 set(release['images'].values()))
                self.assertFalse(any(command[0] in ('inspect', 'build', 'tag') for command in calls))
                runtime = self.home / f'services/firecrawl-{service}/shared/runtime.env'
                self.assertIn(image_key + '=' + release['images'][image_key], runtime.read_text().splitlines())
                self.assertIn('POSTGRES_INIT_DB=postgres', runtime.read_text().splitlines())

    def test_manifest_is_required_before_docker_for_a_fresh_install(self):
        result = self.deploy('browser')
        self.assertIn('--release-images is required', result.stderr)
        self.assertFalse((self.home / 'docker.calls').exists())
        self.assertFalse((self.home / 'services').exists())

    def test_rejects_source_drift_mutable_tags_and_missing_images(self):
        release = self.manifest('monitor')
        missing = release['images']['MONITOR_IMAGE']
        result = self.deploy('monitor', '--check-images', '--release-images', self.write_manifest(release),
                             env={**self.env, 'MISSING_IMAGE': missing})
        self.assertIn('reviewed extension image unavailable: MONITOR_IMAGE', result.stderr)
        release['images']['MONITOR_IMAGE'] = 'monitor:latest'
        result = self.deploy('monitor', '--check-images', '--release-images', self.write_manifest(release))
        self.assertIn('must be exact local Docker image IDs', result.stderr)
        release['files']['nuq.sql'] = '0' * 64
        result = self.deploy('monitor', '--check-images', '--release-images', self.write_manifest(release))
        self.assertIn('file mismatch: nuq.sql', result.stderr)
        self.assertFalse((self.home / 'services').exists())

    def test_existing_pins_cannot_be_replaced_by_the_installer(self):
        release = self.manifest('monitor')
        with self.assertRaisesRegex(ValueError, 'use its upgrade owner'):
            install_images(release, {'MONITOR_IMAGE': 'different'})
        self.assertEqual(install_images(release, release['images']), release['images'])
        legacy = {'BASE_API': 'sha256:' + 'a' * 64}
        self.assertEqual(install_images(None, legacy), legacy)

    def test_switch_replaces_application_images_but_never_storage(self):
        release = self.manifest('browser')
        installed = {**release['images'], 'BROWSER_API_IMAGE': 'sha256:' + 'e' * 64,
                     'BROWSER_IMAGE': 'sha256:' + 'd' * 64, 'APP_DB_PASSWORD': 'kept'}
        self.assertEqual(install_images(release, installed, switch=True),
                         {**installed, **release['images']})
        with self.assertRaisesRegex(ValueError, 'storage images differ'):
            install_images(release, {**installed, 'POSTGRES_IMAGE': 'sha256:' + 'c' * 64}, switch=True)

    def test_switch_installer_updates_the_api_pin_and_rejects_storage_changes(self):
        release = self.manifest('monitor')
        path = self.write_manifest(release)
        runtime = self.home / 'services/firecrawl-monitor/shared/runtime.env'
        runtime.parent.mkdir(parents=True)
        previous = 'sha256:' + 'e' * 64
        runtime.write_text(''.join(f'{key}={image}\n' for key, image in
                                   {**release['images'], 'MONITOR_IMAGE': previous}.items()))
        result = self.deploy('monitor', '--release-images', path)
        self.assertIn('use its upgrade owner', result.stderr)
        result = self.deploy('monitor', '--release-images', path, '--switch',
                             env={**self.env, 'LIVE_IMAGE': previous})
        self.assertIn('exit status 73', result.stderr)
        self.assertIn('MONITOR_IMAGE=' + release['images']['MONITOR_IMAGE'], runtime.read_text().splitlines())
        storage = runtime.read_text().replace(release['images']['REDIS_IMAGE'], 'sha256:' + 'c' * 64)
        runtime.write_text(storage)
        (self.home / 'docker.calls').unlink()
        result = self.deploy('monitor', '--release-images', path, '--switch')
        self.assertIn('storage images differ', result.stderr)
        self.assertFalse(any(command[0] == 'compose' for command in self.calls()))
        self.assertEqual(runtime.read_text(), storage)

    def test_switch_owner_refuses_storage_changes_before_touching_the_extension(self):
        release = self.manifest('monitor')
        (self.source / 'release-images.json').write_text(json.dumps(release))
        ext = extension_switch.Extension('monitor', home=self.home)
        ext.state.mkdir(parents=True)
        ext.envfile.write_text(''.join(f'{key}={image}\n' for key, image in
                                       {**release['images'], 'RABBIT_IMAGE': 'sha256:' + 'c' * 64}.items()))
        with mock.patch.dict(os.environ, {'PATH': self.env['PATH'], 'HOME': str(self.home)}):
            with self.assertRaisesRegex(RuntimeError, 'changes storage images'):
                extension_switch.switch(ext, self.source, ext.state / 'switches/first')
        self.assertFalse((ext.state / 'switches').exists())
        self.assertFalse(any(command[0] in ('compose', 'ps') for command in self.calls()))

    def test_retry_rejects_newer_running_image_before_compose(self):
        release = self.manifest('monitor')
        path = self.write_manifest(release)
        runtime = self.home / 'services/firecrawl-monitor/shared/runtime.env'
        runtime.parent.mkdir(parents=True)
        original = ''.join(f'{key}={image}\n' for key, image in release['images'].items())
        runtime.write_text(original)
        result = self.deploy('monitor', '--release-images', path,
                             env={**self.env, 'LIVE_IMAGE': 'sha256:' + 'f' * 64})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('running extension differs from the release', result.stderr)
        self.assertFalse(any(command[0] == 'compose' for command in self.calls()))
        self.assertEqual(runtime.read_text(), original)

    def test_parallel_installations_use_one_lock_per_extension(self):
        lock = lock_installation(self.home)
        try:
            with self.assertRaises(BlockingIOError):
                lock_installation(self.home)
        finally:
            os.close(lock)
        os.close(lock_installation(self.home))

    def test_app_database_starts_from_clean_template_and_is_retained_on_retry(self):
        for output in ('', '1\n'):
            calls = []

            def compose(*args, **kwargs):
                calls.append(args[-1])
                return SimpleNamespace(stdout=output)

            ensure_app_database(compose, 'browser')
            self.assertEqual(calls, ["SELECT 1 FROM pg_database WHERE datname = 'browser'"]
                             + ([] if output else ['CREATE DATABASE browser TEMPLATE template0']))
