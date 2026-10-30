import contextlib
import copy
import importlib.util
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('promote', Path(__file__).with_name('promote.py'))
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


def image(n): return 'sha256:' + str(n) * 64


class PureTests(unittest.TestCase):
    def test_env_only_replaces_named_lines_and_rejects_ambiguity(self):
        old = b'# keep\r\nSECRET=a=b\r\nBASE_API=old\r\nMONITOR_IMAGE=old\n'
        result = p.replace_env(old, {'BASE_API': image(1), 'MONITOR_IMAGE': image(2)})
        self.assertIn(b'SECRET=a=b\r\n', result)
        self.assertTrue(result.startswith(b'# keep\r\n'))
        with self.assertRaises(p.Stop): p.replace_env(old + b'BASE_API=other\n', {'BASE_API': image(1)})
        with self.assertRaises(p.Stop): p.replace_env(old, {'MISSING': image(1)})

    def test_candidate_rejects_wrong_parent_and_unresolvable_label(self):
        old, new = image(1), image(2)
        obj = {'Id': new, 'Config': {'Labels': {'org.firecrawl.patch.base-image-id': old,
                                                  'org.firecrawl.patch.base': 'local:old'}}}
        p.checked_candidate(new, old, lambda ref: obj if ref == new else {'Id': old})
        with self.assertRaises(p.Stop): p.checked_candidate(new, image(3), lambda _: obj)
        with self.assertRaises(p.Stop): p.checked_candidate(new, old, lambda ref: obj if ref == new else {'Id': image(3)})

    def test_core_override_pins_all_images_without_altering_other_fields(self):
        source = {'services': {'api': {'environment': {'A': 'B'}}, 'db': {'image': 'old'}}, 'volumes': {'data': {}}}
        result = p.candidate_override(source, {'api': image(1), 'db': image(2)}, image(3))
        self.assertEqual(result['services']['api'], {'environment': {'A': 'B'}, 'image': image(3)})
        self.assertEqual(result['services']['db']['image'], image(2))
        self.assertEqual(source['services']['db']['image'], 'old')

    def test_container_comparison_rejects_runtime_change_but_ignores_compose_hash(self):
        old = {'Config': {'Image': image(1), 'Hostname': 'a', 'Env': ['SECRET=x'],
                          'Labels': {'com.docker.compose.config-hash': 'old'}}, 'HostConfig': {'Privileged': False}}
        new = copy.deepcopy(old); new['Config']['Image'] = image(2); new['Config']['Hostname'] = 'b'
        new['Config']['Labels']['com.docker.compose.config-hash'] = 'new'
        self.assertEqual(p.api_configuration(old), p.api_configuration(new))
        old['Config']['Env']=['A=1','B=2']; new['Config']['Env']=['B=2','A=1']
        new['Config']['Labels']['com.docker.compose.replace']='api-1'
        self.assertEqual(p.api_configuration(old), p.api_configuration(new))
        new['Config']['Env']=['A=1','A=2']
        with self.assertRaises(p.Stop): p.api_configuration(new)
        new['Config']['Env']=['A=1','B=2']
        new['HostConfig']['Privileged'] = True
        self.assertNotEqual(p.api_configuration(old), p.api_configuration(new))


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.updater, self.browser, self.monitor = (root / x for x in ('updater', 'browser', 'monitor'))
        for directory in (self.updater, self.browser, self.monitor): directory.mkdir()
        self.old = dict(zip(p.PROJECTS, (image(1), image(2), image(3))))
        self.new = dict(zip(p.PROJECTS, (image(4), image(5), image(6))))
        self.other = image(7); self.snapshots = {}; self.calls = []
        for project in p.PROJECTS:
            work = root / project; work.mkdir()
            files = []
            for i in range(3 if project == 'firecrawl' else 1):
                file = work / ('config' + str(i) + ('.json' if i == 2 else '.yaml'))
                file.write_text(json.dumps({'services': {'api': {}, 'db': {}}}) if i == 2 else 'original')
                files.append(str(file))
            labels = {'com.docker.compose.project.config_files': ','.join(files),
                      'com.docker.compose.project.working_dir': str(work)}
            def row(name, img): return {'Id': name, 'Image': img, 'State': {'Running': True, 'StartedAt': 'before'},
                                        'Config': {'Labels': labels}, 'Mounts': []}
            self.snapshots[project] = {'project': project, 'paths': files, 'workdir': str(work),
                                       'services': {'api': row(project+'-api', self.old[project]),
                                                    'db': row(project+'-db', self.other)}}
        (root / 'firecrawl' / '.env').write_text('SECRET=core-secret\n')
        for directory, key, project in [(self.monitor, 'MONITOR_IMAGE', 'firecrawl-monitor'),
                                        (self.browser, 'BROWSER_API_IMAGE', 'firecrawl-browser')]:
            (directory / 'runtime.env').write_text(f'SECRET=private\nBASE_API={self.old["firecrawl"]}\n{key}={self.old[project]}\n')
        def inspect(kind, ref):
            if ref in self.new.values():
                project = next(k for k, v in self.new.items() if v == ref)
                return {'Id': ref, 'Config': {'Labels': {'org.firecrawl.patch.base-image-id': self.old[project],
                          'org.firecrawl.patch.base': self.old[project]}}}
            return {'Id': ref}
        def render(snapshot, files=None, envfile=None):
            project = snapshot['project']; api = self.old[project]
            if project == 'firecrawl' and files:
                api = json.loads(Path(files[2]).read_text())['services']['api']['image']
            if project != 'firecrawl' and envfile:
                key = 'MONITOR_IMAGE' if project == 'firecrawl-monitor' else 'BROWSER_API_IMAGE'
                api = p.env_value(Path(envfile).read_bytes(), key)
            return {'services': {'api': {'image': api, 'environment': {'SECRET': 'private'}}, 'db': {'image': self.other}}}
        def compose(project, files, workdir, envfile, *args):
            self.calls.append((project, args))
            self.snapshots[project]['services']['api']['Image'] = self.new[project]
            return ''
        patches = [mock.patch.object(p, 'UPDATER', self.updater), mock.patch.object(p, 'BROWSER', self.browser),
                   mock.patch.object(p, 'MONITOR', self.monitor), mock.patch.object(p, 'locks', lambda: contextlib.nullcontext()),
                   mock.patch.object(p, 'project_snapshot', lambda project: copy.deepcopy(self.snapshots[project])),
                   mock.patch.object(p, 'inspect', inspect), mock.patch.object(p, 'render', render),
                   mock.patch.object(p, 'compose', compose), mock.patch.object(p, 'active_sessions', return_value=False),
                   mock.patch.object(p, 'manager_pid', return_value=123), mock.patch.object(p, 'freeze'),
                   mock.patch.object(p, 'start_thaw_watchdog', return_value=None),
                   mock.patch.object(p.os, 'kill'), mock.patch.object(signal, 'SIGCONT', 18, create=True),
                   mock.patch.object(p.os, 'fchmod', create=True),
                   mock.patch.object(p.os, 'fchown', create=True),
                   mock.patch.object(p, 'readiness', return_value=True)]
        for patch in patches: patch.start(); self.addCleanup(patch.stop)

    def test_success_changes_only_api_and_saves_private_evidence(self):
        with mock.patch('builtins.print') as output: p.promote(self.new)
        self.assertEqual([call[0] for call in self.calls], list(p.PROJECTS))
        for _, args in self.calls:
            self.assertIn('--no-deps', args); self.assertIn('--no-build', args); self.assertEqual(args[-1], 'api')
        receipt_dir = next(self.updater.glob('patch-promotion-*'))
        receipt = json.loads((receipt_dir / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'complete')
        self.assertIn(b'SECRET=private', (receipt_dir / 'firecrawl-monitor-runtime.env').read_bytes())
        self.assertNotIn('private', json.dumps(receipt)); self.assertNotIn('private', str(output.call_args))
        self.assertFalse((self.browser / 'admission-paused').exists())
        self.assertEqual(p.env_value((self.monitor / 'runtime.env').read_bytes(), 'BASE_API'), self.new['firecrawl'])
        self.assertEqual(json.loads(Path(receipt['candidate_file']).read_text())['services']['db']['image'], self.other)

    def test_failure_after_pause_leaves_gate_and_records_partial_state(self):
        with mock.patch.object(p, 'compose', side_effect=p.Stop('simulated failure')):
            with self.assertRaises(p.Stop): p.promote(self.new)
        receipt_dir = next(self.updater.glob('patch-promotion-*'))
        self.assertEqual(json.loads((receipt_dir / 'receipt.json').read_text())['status'], 'failed-manual-intervention')
        self.assertTrue((self.browser / 'admission-paused').exists())
        self.assertEqual(p.env_value((self.browser / 'runtime.env').read_bytes(), 'BROWSER_API_IMAGE'), self.new['firecrawl-browser'])

    def test_active_session_stops_before_env_mutation(self):
        with mock.patch.object(p, 'active_sessions', return_value=True):
            with self.assertRaises(p.Stop): p.promote(self.new)
        self.assertFalse(self.calls)
        self.assertFalse((self.browser / 'admission-paused').exists())
        self.assertEqual(p.env_value((self.monitor / 'runtime.env').read_bytes(), 'MONITOR_IMAGE'), self.old['firecrawl-monitor'])

    def test_wrong_persistent_image_rejects_before_creating_candidate(self):
        path = self.monitor / 'runtime.env'
        path.write_bytes(path.read_bytes().replace(self.old['firecrawl'].encode(), image(8).encode()))
        with self.assertRaises(p.Stop): p.promote(self.new)
        self.assertFalse(self.calls)
        self.assertFalse(list(self.updater.glob('patch-promotion-*')))

    def test_readiness_failure_stops_before_remaining_apis(self):
        with mock.patch.object(p, 'readiness', side_effect=p.Stop('simulated readiness failure')):
            with self.assertRaises(p.Stop): p.promote(self.new)
        self.assertEqual([call[0] for call in self.calls], ['firecrawl'])
        self.assertTrue((self.browser / 'admission-paused').exists())
        receipt_dir = next(self.updater.glob('patch-promotion-*'))
        record = json.loads((receipt_dir / 'receipt.json').read_text())
        self.assertEqual(record['failure_stage'], 'recreating-firecrawl')


if __name__ == '__main__': unittest.main()
