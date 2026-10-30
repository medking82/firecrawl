import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import advisory_owner as owner
import advisory_trivy as scanner

NOW = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)


def db_meta(updated=NOW, next_update=None, version=2):
    stamp = lambda x: x.isoformat().replace('+00:00', 'Z')
    return {'Version': version, 'UpdatedAt': stamp(updated),
            'DownloadedAt': stamp(NOW), 'NextUpdate': stamp(next_update or (NOW + dt.timedelta(hours=12)))}


class Tests(unittest.TestCase):
    def test_freshness_rejects_stale_malformed_future_and_wrong_schema(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'metadata.json'
            p.write_text(json.dumps(db_meta()), encoding='utf-8')
            self.assertEqual(owner.metadata(p, 2, NOW)['version'], 2)
            for data in [db_meta(updated=NOW-dt.timedelta(hours=49)), db_meta(next_update=NOW),
                         db_meta(updated=NOW+dt.timedelta(hours=1)), db_meta(version=1),
                         {'Version': 2, 'UpdatedAt': 'broken'}, {'Version': True}]:
                p.write_text(json.dumps(data), encoding='utf-8')
                with self.subTest(data=data), self.assertRaises(scanner.ScanError):
                    owner.metadata(p, 2, NOW)
            p.write_text('{', encoding='utf-8')
            with self.assertRaises(scanner.ScanError):
                owner.metadata(p, 2, NOW)

    def test_offline_scan_and_java_detection(self):
        captured = []
        def execute(cmd):
            captured.append(cmd)
            return b'{"Results": []}'
        with patch.object(owner.os, 'getuid', return_value=1000, create=True), patch.object(owner.os, 'getgid', return_value=1000, create=True):
            run = owner.offline_executor(Path('/private/stage'), execute)
            run(['docker', 'run', scanner.TRIVY, '--cache-dir', '/tmp/trivy', 'image'])
            self.assertIn('--network=none', captured[0])
            self.assertIn('--skip-db-update', captured[0])
            self.assertIn('--skip-java-db-update', captured[0])
            self.assertIn('--offline-scan', captured[0])
            run = owner.offline_executor(Path('/private/stage'), lambda _: b'{"Results":[{"Type":"jar"}]}')
            with self.assertRaises(scanner.ScanError):
                run(['docker', 'run', scanner.TRIVY, '--cache-dir', '/tmp/trivy', 'image'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and symlink semantics')
    def test_refresh_validates_before_atomic_promotion(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / 'cache'
            def execute(cmd):
                if cmd[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps(scanner.TRIVY).encode()
                mount = cmd[cmd.index('--mount') + 1]
                directory = Path(mount.split('src=', 1)[1].split(',dst=', 1)[0]) / 'db'
                directory.mkdir()
                (directory / 'trivy.db').write_bytes(b'fixture')
                (directory / 'metadata.json').write_text(json.dumps(db_meta()))
                return b''
            generation = owner.refresh_cache(cache, execute, NOW)
            self.assertEqual((cache / 'current').resolve(), generation)
            prior = os.readlink(cache / 'current')
            def fail(cmd):
                if cmd[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps(scanner.TRIVY).encode()
                raise scanner.ScanError('refresh failure')
            with self.assertRaises(scanner.ScanError):
                owner.refresh_cache(cache, fail, NOW)
            self.assertEqual(os.readlink(cache / 'current'), prior)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink and permissions semantics')
    def test_retains_only_current_and_previous_without_following_links(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / 'cache'
            def execute(cmd):
                if cmd[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps(scanner.TRIVY).encode()
                mount = cmd[cmd.index('--mount') + 1]
                directory = Path(mount.split('src=', 1)[1].split(',dst=', 1)[0]) / 'db'
                directory.mkdir()
                (directory / 'trivy.db').write_bytes(b'fixture')
                (directory / 'metadata.json').write_text(json.dumps(db_meta()))
                return b''
            first = owner.refresh_cache(cache, execute, NOW)
            second = owner.refresh_cache(cache, execute, NOW)
            third = owner.refresh_cache(cache, execute, NOW)
            self.assertFalse(first.exists())
            self.assertTrue(second.is_dir())
            self.assertTrue(third.is_dir())
            self.assertEqual((cache / 'current').resolve(), third)
            outside = Path(d) / 'outside'
            outside.mkdir()
            (outside / 'sentinel').write_text('keep')
            suspicious = cache / 'generations' / ('gen-' + 'f' * 32)
            suspicious.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(scanner.ScanError):
                owner.refresh_cache(cache, execute, NOW)
            self.assertTrue((outside / 'sentinel').exists())
            self.assertTrue(suspicious.is_symlink())

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink semantics')
    def test_invalid_pointer_and_unknown_files_are_not_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'cache'
            cache.mkdir(mode=0o700)
            generations = cache / 'generations'
            generations.mkdir(mode=0o700)
            unknown = generations / 'unrelated'
            unknown.write_text('keep')
            owner.retain_generations(generations, set())
            self.assertEqual(unknown.read_text(), 'keep')
            (cache / 'current').symlink_to('../outside')
            with self.assertRaises(scanner.ScanError):
                owner.current_generation(cache, generations)

    @unittest.skipUnless(os.name == 'posix', 'POSIX atomic and permissions semantics')
    def test_private_atomic_report_replaces_previous(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            owner.atomic_report(path, {'status': 'findings', 'findings_count': 138})
            owner.atomic_report(path, {'status': 'error'})
            self.assertEqual(json.loads((path / 'latest.json').read_text()), {'status': 'error'})
            self.assertEqual((path / 'latest.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in path.iterdir()], ['latest.json'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX locking and permissions semantics')
    def test_failure_replaces_clean_report_and_returns_error(self):
        with tempfile.TemporaryDirectory() as d, patch.object(Path, 'home', return_value=Path(d)):
            old_mask = os.umask(0o077)
            try:
                with patch.object(owner, 'perform', return_value={'status': 'no_high_critical_findings', 'findings_count': 0}):
                    self.assertEqual(owner.main(), 0)
                with patch.object(owner, 'perform', side_effect=scanner.ScanError('secret should not appear')):
                    self.assertEqual(owner.main(), 2)
                report = (Path(d) / '.local/state/firecrawl-advisory/reports/latest.json').read_text()
                self.assertEqual(json.loads(report)['status'], 'error')
                self.assertNotIn('secret should not appear', report)
            finally:
                os.umask(old_mask)

    def test_three_targets_and_change_detection(self):
        initial = ('sha256:' + 'a' * 64, 'b' * 64)
        def fake_scan(mode, name, execute):
            return {'image_id': initial[0], 'findings': [{'severity': 'HIGH'}]}
        with tempfile.TemporaryDirectory() as d, patch.object(owner, 'refresh_cache', return_value=Path(d)), \
             patch.object(owner, 'stage_cache', return_value={'db': 'fixture'}), \
             patch.object(owner, 'metadata', return_value={}), patch.object(scanner, 'capture', return_value=initial), \
             patch.object(scanner, 'scan', side_effect=fake_scan):
            result = owner.perform(Path(d))
            self.assertEqual(result['findings_count'], 3)
            self.assertEqual([r['container'] for r in result['images']], list(owner.TARGETS))
            self.assertEqual(result['status'], 'findings')


if __name__ == '__main__':
    unittest.main()
