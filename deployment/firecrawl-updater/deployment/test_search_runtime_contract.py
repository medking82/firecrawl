import copy
import hashlib
import subprocess
import sys
import unittest
from pathlib import Path

from deployment import search_runtime_contract as contract

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent.parent / 'search-sidecar' / 'runtime'


def config(profile):
    services = copy.deepcopy(contract.EXPECTED['services'])
    services['searxng']['environment'] = {'SEARXNG_SECRET': 'x'}
    api = copy.deepcopy(contract.EXPECTED['profiles'][profile]['api'])
    api['environment'] = {**api['environment'], 'USE_DB_AUTHENTICATION': 'false'}
    services['api'] = api
    return {'services': services, 'networks': copy.deepcopy(contract.EXPECTED['networks'])}


class SearchRuntimeContractTests(unittest.TestCase):
    def test_both_profiles_are_admitted(self):
        for profile in ('sidecar', 'preload'):
            with self.subTest(profile=profile):
                self.assertEqual(contract.search_profile(config(profile)), profile)
                files = contract.search_contract(config(profile), check_files=False)
                self.assertEqual(files, contract.EXPECTED['profiles'][profile]['files'])

    def test_only_sidecar_pins_worker_image(self):
        self.assertTrue(contract.search_worker_pinned(config('sidecar')))
        self.assertFalse(contract.search_worker_pinned(config('preload')))

    def test_sidecar_core_loads_no_search_code(self):
        api = contract.EXPECTED['profiles']['sidecar']['api']
        self.assertNotIn('NODE_OPTIONS', api['environment'])
        self.assertNotIn('SEARCH_WORKER_URL', api['environment'])
        self.assertEqual([v['type'] for v in api['volumes']], ['volume'])
        self.assertIn('html.duckduckgo.com=127.0.0.1', api['extra_hosts'])

    def test_mixed_or_partial_profiles_fail_closed(self):
        mixed = config('sidecar')
        mixed['services']['api']['environment']['NODE_OPTIONS'] = '--require=/app/searxng-timeout.cjs'
        unblocked = config('sidecar')
        unblocked['services']['api']['extra_hosts'] = ['host.docker.internal=host-gateway']
        stale_mount = config('sidecar')
        stale_mount['services']['api']['volumes'] = contract.EXPECTED['profiles']['preload']['api']['volumes']
        preload_unblocked_worker = config('preload')
        preload_unblocked_worker['services']['api']['environment']['SEARXNG_ENDPOINT'] = 'http://searxng:8080/'
        no_worker = config('sidecar')
        del no_worker['services']['search-worker']
        for name, value, error in (('mixed', mixed, 'search_environment_drift'),
                                   ('unblocked', unblocked, 'search_api_drift'),
                                   ('stale_mount', stale_mount, 'search_api_drift'),
                                   ('preload_endpoint', preload_unblocked_worker, 'search_environment_drift'),
                                   ('no_worker', no_worker, 'search_service_missing')):
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, error):
                    contract.search_contract(value, check_files=False)

    def test_sidecar_files_are_the_versioned_runtime(self):
        for path, digest in contract.EXPECTED['profiles']['sidecar']['files'].items():
            local = RUNTIME / Path(path).name
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), digest)

    def test_preload_rollback_differs_only_in_worker(self):
        sidecar = contract.EXPECTED['profiles']['sidecar']['files']
        preload = contract.EXPECTED['profiles']['preload']['files']
        self.assertEqual(set(sidecar), set(preload))
        self.assertEqual({k for k in sidecar if sidecar[k] != preload[k]},
                         {'/opt/firecrawl-search/search-worker.cjs'})

    def test_embedded_copies_match_source(self):
        subprocess.run([sys.executable, '-I', '-B', str(HERE / 'render_search_contract.py'), '--check'], check=True)


if __name__ == '__main__':
    unittest.main()
