"""Mock the production boundary; no Docker, service, or database is touched."""
import contextlib
import copy
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recovery_cutover as c


def image(n):
    return 'sha256:' + str(n) * 64


class CutoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.updater = root / 'updater'; self.updater.mkdir()
        self.browser = root / 'browser'; self.browser.mkdir()
        self.calls = []; self.snapshots = {}
        self.configs = {}
        for index, name in enumerate(c.p.PROJECTS, 1):
            work = root / name; work.mkdir()
            config = work / 'compose.json'; config.write_text('{}')
            env = work / '.env'; env.write_text('PRIVATE=not-printed')
            def row(service, num):
                return {'Id': name + '-' + service, 'Image': image(num),
                        'State': {'Running': True, 'StartedAt':'2026-10-30T00:00:00Z'}, 'Mounts': ([{'Type': 'volume', 'Name': name + '-volume',
                        'Destination': '/var/lib/postgresql/data', 'RW': True}] if service == c.DB[name] else [])}
            services = {'api': row('api', index), c.DB[name]: row(c.DB[name], 4)}
            if name == 'firecrawl': services['playwright-service'] = row('playwright-service', 5)
            self.snapshots[name] = {'project': name, 'paths': [str(config)], 'workdir': str(work),
                                    'envfile': str(env), 'services': services}
            self.configs[name] = {'services': {svc: {'image': image(6 if svc == c.DB[name] else 7)}
                                                for svc in services if svc != 'api'}}
        self.images = {image(6): {'Id': image(6)}, image(7): {'Id': image(7)}}
        self.rows = {row['Id']: copy.deepcopy(row) for snap in self.snapshots.values() for row in snap['services'].values()}
        def inspect(kind, ref):
            return copy.deepcopy(self.images[ref] if kind == 'image' else self.rows[ref])
        def run(args, **kwargs):
            self.calls.append(tuple(args))
            if args[:2] == ['docker', 'stop']:
                self.rows[args[-1]]['State']['Running'] = False
            elif args[:2] == ['docker', 'start']:
                self.rows[args[-1]]['State']['Running'] = True
            elif args[:3] == ['docker', 'ps', '-aq']:
                project = args[4].split('=', 2)[-1]; service = args[6].split('=', 2)[-1]
                return project + '-' + service + '-new'
            return ''
        def compose(name, files, workdir, envfile, *args):
            self.calls.append(('compose', name) + args)
            service = args[-1]; old = self.snapshots[name]['services'][service]
            row = copy.deepcopy(old); row['Id'] = name + '-' + service + '-new'
            row['Image'] = image(7 if service == 'playwright-service' else 6)
            self.rows[row['Id']] = row
        patches = [mock.patch.object(c.p, 'UPDATER', self.updater), mock.patch.object(c.p, 'BROWSER', self.browser),
                   mock.patch.object(c, 'ENV', {}), mock.patch.object(c, 'locks', lambda: contextlib.nullcontext()),
                   mock.patch.object(c, 'verify_backup', return_value={'files_verified': 8}),
                   mock.patch.object(c.p, 'project_snapshot', side_effect=lambda name: copy.deepcopy(self.snapshots[name])),
                   mock.patch.object(c.p, 'render', side_effect=lambda snap, envfile=None: copy.deepcopy(self.configs[snap['project']])),
                   mock.patch.object(c.p, 'inspect', side_effect=inspect), mock.patch.object(c.p, 'run', side_effect=run),
                   mock.patch.object(c.p, 'compose', side_effect=compose), mock.patch.object(c, 'fingerprint', return_value='ok'),
                   mock.patch.object(c, 'idle', return_value=True), mock.patch.object(c, 'wait_for_drain'), mock.patch.object(c, 'db_ready'),
                   mock.patch.object(c, 'wait_api'), mock.patch.object(c.p, 'active_sessions', return_value=False),
                   mock.patch.object(c.p, 'manager_pid', return_value=123), mock.patch.object(c.p, 'freeze'),
                   mock.patch.object(c.p, 'start_thaw_watchdog', return_value=None), mock.patch.object(c.os, 'kill'),
                   mock.patch.object(c.os, 'fchmod', create=True),
                   mock.patch.object(c.signal, 'SIGCONT', 18, create=True)]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)

    def test_success_uses_existing_api_and_only_target_services(self):
        receipt = c.cutover('/private/backup', 3600)
        self.assertEqual(json.loads(receipt.read_text())['status'], 'complete')
        recreates = [a for a in self.calls if a[0] == 'compose']
        self.assertEqual([a[-1] for a in recreates], ['nuq-postgres', 'playwright-service', 'app-db', 'app-db'])
        self.assertTrue(all('--no-deps' in a and '--pull' in a and 'never' in a for a in recreates))
        self.assertEqual(len([a for a in self.calls if a[:2] == ('docker', 'start')]), 3)
        self.assertFalse((self.browser / 'admission-paused').exists())

    def test_busy_restarts_exact_api_and_never_recreates(self):
        with mock.patch.object(c, 'idle', return_value=False):
            with self.assertRaises(c.p.Stop): c.cutover('/private/backup', 3600)
        self.assertIn(('docker', 'start', 'firecrawl-api'), self.calls)
        self.assertFalse(any(a[0] == 'compose' for a in self.calls))
        self.assertTrue((self.browser / 'admission-paused').exists())
        receipt = next(self.updater.glob('recovery-cutover-*/receipt.json'))
        self.assertEqual(json.loads(receipt.read_text())['status'], 'failed-manual-intervention')

    def test_volume_change_stops_without_api_restart_or_rollback(self):
        with mock.patch.object(c, 'verify_same_volume', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError): c.cutover('/private/backup', 3600)
        self.assertFalse(any(a[:2] == ('docker', 'start') for a in self.calls))
        self.assertTrue((self.browser / 'admission-paused').exists())

    def test_idle_query_failure_restores_exact_api(self):
        with mock.patch.object(c, 'idle', side_effect=c.p.Stop('query failed')):
            with self.assertRaises(c.p.Stop): c.cutover('/private/backup', 3600)
        self.assertIn(('docker', 'start', 'firecrawl-api'), self.calls)
        self.assertFalse(any(a[0] == 'compose' for a in self.calls))

    def test_db_validation_failure_preserves_stopped_api(self):
        with mock.patch.object(c, 'db_ready', side_effect=c.p.Stop('schema failed')):
            with self.assertRaises(c.p.Stop): c.cutover('/private/backup', 3600)
        self.assertFalse(any(a[:2] == ('docker', 'start') for a in self.calls))
        self.assertTrue((self.browser / 'admission-paused').exists())

    def test_backup_failure_before_mutations(self):
        with mock.patch.object(c, 'verify_backup', side_effect=ValueError('invalid')):
            with self.assertRaises(ValueError): c.cutover('/private/backup', 3600)
        self.assertEqual(self.calls, [])


class IdleTest(unittest.TestCase):
    def test_busy_activity_rejects_without_claiming_empty(self):
        with mock.patch.object(c, 'sql', return_value='1'), mock.patch.object(c, 'queues_empty') as queues:
            self.assertFalse(c.idle('firecrawl', 'db'))
            queues.assert_not_called()

    def test_monitor_queue_checked_after_nuq(self):
        with mock.patch.object(c, 'sql', side_effect=['0', '1']):
            self.assertFalse(c.queues_empty('firecrawl-monitor', 'db'))

    def test_fingerprint_refuses_difference_and_avoids_anonymous_volume(self):
        with mock.patch.object(c.p, 'inspect', return_value={'Config': {'Volumes': {'/var/lib/postgresql/data': {}}}}), \
             mock.patch.object(c.p, 'run', side_effect=['old\nold', 'new\nnew']) as run:
            with self.assertRaises(c.p.Stop): c.fingerprint('container', image(6), c.PG_PATHS)
            args = run.call_args_list[1].args[0]
            self.assertIn('--tmpfs', args)
            self.assertIn('--network', args)

    def test_unexpected_image_volume_refused(self):
        with mock.patch.object(c.p, 'inspect', return_value={'Config': {'Volumes': {'/other': {}}}}):
            with self.assertRaises(c.p.Stop): c.fingerprint('container', image(6), c.PG_PATHS)


if __name__ == '__main__':
    unittest.main()
