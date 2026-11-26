"""The isolated candidate trial never shares state, ports or credentials with the live project."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).with_name('firecrawl_update.sh')
TEXT = SOURCE.read_text()
BODY = TEXT.split("<<'PYTRIAL'\n", 1)[1].split('\nPYTRIAL\n', 1)[0]
NAMESPACE = {'__name__': 'candidate_trial'}
exec(compile(BODY, 'candidate_trial', 'exec'), NAMESPACE)
trial_config = NAMESPACE['trial_config']
wait_for_trial_api = NAMESPACE['wait_for_trial_api']
teardown = NAMESPACE['teardown']

IMAGE = 'sha256:' + 'a' * 64
LIVE = {
    'name': 'firecrawl',
    'services': {
        'api': {'image': IMAGE, 'build': {'context': '/x'}, 'restart': 'unless-stopped',
                'ports': [{'host_ip': '127.0.0.1', 'published': '3002', 'target': 3002}],
                'environment': {'PORT': '3002', 'POSTGRES_PASSWORD': 'live-secret', 'BULL_AUTH_KEY': 'live-bull',
                                'NUM_WORKERS_PER_QUEUE': '8', 'MAX_CONCURRENT_JOBS': '1',
                                'SEARXNG_ENDPOINT': 'http://search-worker:8081', 'SEARCH_TRACE': '1',
                                'OPENAI_BASE_URL': 'http://host.docker.internal:10180/v1'},
                'networks': {'backend': None, 'search': {'ipv4_address': '172.30.247.10'}},
                'volumes': [{'type': 'volume', 'source': 'fdb-cluster-file', 'target': '/var/fdb', 'read_only': True},
                            {'type': 'bind', 'source': '/opt/firecrawl-search/searxng-timeout.cjs',
                             'target': '/app/searxng-timeout.cjs', 'read_only': True}],
                'depends_on': {'nuq-postgres': {'condition': 'service_healthy'},
                               'search-worker': {'condition': 'service_started'}},
                'extra_hosts': ['html.duckduckgo.com=127.0.0.1']},
        'nuq-postgres': {'image': IMAGE, 'environment': {'POSTGRES_PASSWORD': 'live-secret'},
                         'networks': {'backend': None}},
        'foundationdb': {'image': IMAGE, 'networks': {'backend': None},
                         'volumes': [{'type': 'volume', 'source': 'fdb-data', 'target': '/var/fdb/data'}]},
        'search-worker': {'image': IMAGE, 'networks': {'search': None}},
        'searxng': {'image': IMAGE, 'networks': {'search': None}},
        'search-egress': {'image': IMAGE, 'networks': {'search': None, 'search_egress': None}},
    },
    'networks': {'backend': {'name': 'firecrawl_backend'}, 'search': {'name': 'firecrawl_search', 'internal': True},
                 'search_egress': {'name': 'firecrawl_search_egress'}},
    'volumes': {'fdb-cluster-file': {'name': 'firecrawl_fdb-cluster-file'}, 'fdb-data': {'name': 'firecrawl_fdb-data'}},
}
IMAGES = {name: {'image': 'sha256:' + str(index) * 64} for index, name in
          enumerate(['api', 'nuq-postgres', 'foundationdb', 'search-worker', 'searxng', 'search-egress'], 1)}


class CandidateTrialConfigTests(unittest.TestCase):
    def test_trial_is_a_separate_project_with_its_own_state(self):
        trial = trial_config(copy.deepcopy(LIVE), IMAGES, 3012, 'firecrawl-trial')
        self.assertEqual(trial['name'], 'firecrawl-trial')
        self.assertEqual(set(trial['services']), {'api', 'nuq-postgres', 'foundationdb', 'search-worker'})
        # Project-scoped networks and volumes: no explicit names, so nothing resolves to firecrawl_*.
        self.assertEqual(trial['networks'], {'backend': {'driver': 'bridge'}})
        self.assertEqual(trial['volumes'], {'fdb-cluster-file': {}, 'fdb-data': {}})
        self.assertNotIn('firecrawl_', json.dumps(trial['networks']) + json.dumps(trial['volumes']))
        for name, service in trial['services'].items():
            self.assertEqual(service['restart'], 'no')
            self.assertEqual(service['pull_policy'], 'never')
            self.assertNotIn('build', service)
            self.assertNotIn('search', service['networks'])
            self.assertNotIn('container_name', service)
            if name != 'api':
                self.assertNotIn('ports', service)

    def test_candidate_images_credentials_port_and_search_fixture(self):
        trial = trial_config(copy.deepcopy(LIVE), IMAGES, 3012, 'firecrawl-trial')
        api = trial['services']['api']
        self.assertEqual(api['image'], IMAGES['api']['image'])
        self.assertEqual(trial['services']['nuq-postgres']['image'], IMAGES['nuq-postgres']['image'])
        self.assertEqual(api['ports'], [{'mode': 'ingress', 'host_ip': '127.0.0.1', 'target': 3002,
                                         'published': '3012', 'protocol': 'tcp'}])
        env = api['environment']
        password = env['POSTGRES_PASSWORD']
        self.assertNotEqual(password, 'live-secret')
        self.assertEqual(trial['services']['nuq-postgres']['environment']['POSTGRES_PASSWORD'], password)
        self.assertNotEqual(env['BULL_AUTH_KEY'], 'live-bull')
        self.assertEqual((env['NUM_WORKERS_PER_QUEUE'], env['MAX_CONCURRENT_JOBS']), ('2', '1'))
        self.assertEqual(env['SEARXNG_ENDPOINT'], 'http://search-worker:8081')
        self.assertNotIn('SEARCH_TRACE', env)
        self.assertEqual(api['volumes'], [LIVE['services']['api']['volumes'][0]])
        self.assertEqual(api['extra_hosts'], ['html.duckduckgo.com=127.0.0.1'])
        fixture = trial['services']['search-worker']
        self.assertEqual(fixture['image'], IMAGES['api']['image'])
        self.assertEqual((fixture['read_only'], fixture['cap_drop']), (True, ['ALL']))
        self.assertEqual(fixture['networks'], {'backend': None})
        self.assertEqual(set(api['depends_on']), {'nuq-postgres', 'search-worker'})

    def test_fixture_answers_the_searxng_contract(self):
        fixture = NAMESPACE['FIXTURE']
        script = fixture + "\nsetTimeout(async () => {const r = await fetch('http://127.0.0.1:8081/search?q=x&format=json');" \
                           "console.log(JSON.stringify(await r.json())); process.exit(0);}, 200);"
        try:
            result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=20)
        except FileNotFoundError:
            self.skipTest('node unavailable')
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['results'][0]['url'], 'https://example.com/')
        self.assertEqual(set(value['results'][0]), {'url', 'title', 'content'})

    def test_unsafe_inputs_fail_closed(self):
        for project in ('firecrawl', 'other', 'firecrawl-trial_x'):
            with self.subTest(project=project), self.assertRaisesRegex(ValueError, 'trial_project_name'):
                trial_config(copy.deepcopy(LIVE), IMAGES, 3012, project)
        cases = {
            'trial_unsupported_mount': lambda c: c['services']['nuq-postgres'].__setitem__(
                'volumes', [{'type': 'bind', 'source': '/home/marck', 'target': '/data'}]),
            'trial_container_name': lambda c: c['services']['api'].__setitem__('container_name', 'firecrawl-api'),
            'trial_external_volumes': lambda c: c['volumes']['fdb-data'].__setitem__('external', True),
            'trial_image_missing': lambda c: c['services'].__setitem__('new-service', {'networks': {'backend': None}}),
        }
        for error, change in cases.items():
            config = copy.deepcopy(LIVE)
            change(config)
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                trial_config(config, IMAGES, 3012, 'firecrawl-trial')

    def test_readiness_stops_when_trial_api_exits(self):
        for state in ('exited', 'dead'):
            with self.subTest(state=state):
                opener = Mock()
                docker = Mock(return_value=state + '\n')
                with patch.dict(NAMESPACE, run=docker), patch('time.sleep') as sleep:
                    self.assertEqual(wait_for_trial_api('firecrawl-trial', opener, 'http://127.0.0.1:3012'),
                                     'trial_api_exited')
                opener.open.assert_not_called()
                sleep.assert_not_called()
                docker.assert_called_once_with(
                    ['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=firecrawl-trial',
                     '--filter', 'label=com.docker.compose.service=api',
                     '--filter', 'label=com.docker.compose.oneoff=False', '--format', '{{.State}}'], timeout=10)

    def test_slow_api_can_become_ready(self):
        opener = Mock()
        opener.open.side_effect = [ConnectionRefusedError(), opener.open.return_value]
        opener.open.return_value.__enter__ = Mock(return_value=Mock(status=200))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.dict(NAMESPACE, run=Mock(return_value='running\n')), patch('time.sleep') as sleep:
            self.assertIsNone(wait_for_trial_api('firecrawl-trial', opener, 'http://127.0.0.1:3012'))
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once()

    def test_readiness_rejects_missing_or_ambiguous_api(self):
        for states in ('', 'running\nexited\n'):
            with self.subTest(states=states), patch.dict(NAMESPACE, run=Mock(return_value=states)):
                opener = Mock()
                self.assertEqual(wait_for_trial_api('firecrawl-trial', opener, 'http://127.0.0.1:3012'),
                                 'trial_api_container_count')
                opener.open.assert_not_called()

    def test_teardown_refuses_production_and_unrelated_projects(self):
        with patch.dict(NAMESPACE, run=Mock()) as namespace:
            for project in ('firecrawl', 'other', 'firecrawl-trial_x'):
                with self.subTest(project=project), self.assertRaisesRegex(ValueError, 'trial_project_name'):
                    teardown(project, {}, Path('/trial'))
            namespace['run'].assert_not_called()

    def test_teardown_checks_for_lingering_compose_resources(self):
        for outputs, clean in ((('', '', '', ''), True), (('', 'container-id'), False),
                               (('', '', 'network-id'), False), (('', '', '', 'volume-id'), False)):
            with self.subTest(outputs=outputs), patch.dict(NAMESPACE, run=Mock(side_effect=outputs)):
                self.assertEqual(teardown('firecrawl-trial', {}, Path('/trial')), clean)
        with patch.dict(NAMESPACE, run=Mock(side_effect=subprocess.TimeoutExpired('docker', 300))):
            self.assertFalse(teardown('firecrawl-trial', {}, Path('/trial')))

    def test_trial_runs_before_any_live_change_and_cleans_up_on_failure(self):
        cutover = TEXT.split('\ncutover_candidate() {\n', 1)[1].split('\n}\n', 1)[0]
        self.assertLess(cutover.index('trial_candidate'), cutover.index('flock -x -n 9'))
        self.assertLess(cutover.index('trial_candidate'), cutover.index('gate consume'))
        rollback = TEXT.split('\nrollback() {\n', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('[[ "$phase" != trial ]] || trial_teardown', rollback)
        notice = TEXT.split('\nfailure_notice() {\n', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('trial)', notice)
        self.assertIn('live runtime unchanged; approval kept', notice)


if __name__ == '__main__':
    unittest.main()
