"""Search admission remains exact while allowing an image-only update."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deployment.search_runtime_contract import EXPECTED, search_contract, search_image_overlay


def config():
    value = copy.deepcopy(EXPECTED)
    value['services']['api'] = value.pop('api')
    value['services']['searxng']['environment'] = {'SEARXNG_SECRET':'test-only'}
    for name in value['services']:
        value['services'][name]['image'] = 'sha256:'+'a'*64
    return value


class SearchRuntime(unittest.TestCase):
    def test_exact_runtime_and_image_replacement(self):
        c = config()
        self.assertEqual(search_contract(c,False),EXPECTED['files'])
        for n in ('api','search-worker','searxng','search-egress'):
            c['services'][n]['image'] = 'sha256:'+'b'*64
        self.assertEqual(search_contract(c,False),EXPECTED['files'])

    def test_unknown_drift_rejected(self):
        for mutate in (
            lambda c:c['services']['api']['environment'].update(NODE_OPTIONS='--require=/tmp/x'),
            lambda c:c['services']['api']['volumes'][1].update(read_only=False),
            lambda c:c['services']['search-worker'].update(network_mode='host'),
            lambda c:c['services']['search-worker']['networks'].update(backend=None),
            lambda c:c['services']['search-egress']['environment'].update(PROXY='arbitrary'),
            lambda c:c['networks']['search'].update(internal=False),
            lambda c:c['services']['searxng']['environment'].update(NODE_OPTIONS='bad'),
        ):
            c=config();c['services']['search-egress']['environment']={}
            # baseline egress has no environment; only the relevant case adds it.
            c['services']['search-egress'].pop('environment')
            try:mutate(c)
            except KeyError:c['services']['search-egress']['environment']={'PROXY':'arbitrary'}
            with self.assertRaises(ValueError):search_contract(c,False)

    def test_mount_bytes_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'payload';p.write_bytes(b'reviewed')
            expected=copy.deepcopy(EXPECTED);expected['files']={str(p):hashlib.sha256(b'reviewed').hexdigest()}
            with patch.dict(EXPECTED,expected,clear=True):
                self.assertTrue(search_contract(config()))
                p.write_bytes(b'drift')
                with self.assertRaisesRegex(ValueError,'file_drift'):search_contract(config())
                p.unlink();p.symlink_to(Path(t)/'other')
                with self.assertRaisesRegex(ValueError,'file_unavailable'):search_contract(config())

    def test_api_only_image_pin(self):
        with tempfile.TemporaryDirectory() as t, patch.object(Path,'cwd',return_value=Path(t)):
            p=Path(t)/'docker-compose.search-image.json'
            value={'services':{'api':{'image':'sha256:'+'a'*64,'pull_policy':'never'}}}
            p.write_text(json.dumps(value));self.assertTrue(search_image_overlay(p))
            value['services']['api']['environment']={'NODE_OPTIONS':'bad'}
            p.write_text(json.dumps(value));self.assertFalse(search_image_overlay(p))
            self.assertFalse(search_image_overlay(Path(t)/'unknown.json'))

    def test_no_search_runtime_unchanged(self):
        self.assertIsNone(search_contract({'services':{'api':{'environment':{}}}}))

    def test_embedded_contract_has_identical_source(self):
        root=Path(__file__).parent
        source=(root/'search_runtime_contract.py').read_text()
        script=(root/'firecrawl_update.sh').read_text()
        blocks=script.split('# BEGIN SEARCH CONTRACT (generated from search_runtime_contract.py)\n')[1:]
        self.assertEqual(len(blocks),2)
        for block in blocks:self.assertEqual(block.split('# END SEARCH CONTRACT',1)[0],source)


class SearchCandidateIntegration(unittest.TestCase):
    """Exercise the complete candidate/manifest gate with a frozen search fixture."""
    def setUp(self):
        from deployment import test_firecrawl_candidate as candidate
        self.candidate_module = candidate
        self.helper = candidate.CandidateTests('test_exact_reviewed_profile_pins_images_only_after_recheck')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.helper.config['services'].update(config()['services'])
        self.helper.config['services']['api']['environment'].update(
            USE_DB_AUTHENTICATION='false', PLAYWRIGHT_MICROSERVICE_URL='http://fixture')
        self.helper.config['networks'] = copy.deepcopy(EXPECTED['networks'])
        docker = self.helper.root/'docker'
        source = docker.read_text()
        source = source.replace(
            "('firecrawl-nuq-postgres-1','firecrawl-playwright-service-1')",
            "('firecrawl-nuq-postgres-1','firecrawl-playwright-service-1','firecrawl-searxng-1','firecrawl-search-egress-1')")
        source = source.replace(
            "name='nuq-postgres' if args[-1]=='firecrawl-nuq-postgres-1' else 'playwright-service'",
            "name=args[-1][len('firecrawl-'):-len('-1')]")
        docker.write_text(source)
        # Only map fixture filesystem locations; production embedded code is unchanged.
        payload = self.helper.root/'reviewed-search-file'
        payload.write_bytes(b'reviewed-search-fixture')
        self.fixture_files = {str(payload):hashlib.sha256(payload.read_bytes()).hexdigest()}
        original = candidate.embedded
        def fixture_embedded(marker):
            code = original(marker)
            return code.replace('# END SEARCH CONTRACT',
                'EXPECTED["files"] = '+repr(self.fixture_files)+'\n# END SEARCH CONTRACT')
        patcher = patch.object(candidate, 'embedded', fixture_embedded)
        patcher.start();self.addCleanup(patcher.stop)

    def test_search_overlay_and_preload_admitted_and_all_images_pinned(self):
        result = self.helper.candidate()
        self.assertEqual(result.returncode,0,result.stderr)
        report = json.loads((self.helper.evidence/'checks.json').read_text())
        self.assertEqual(report['status'],'checks_passed')
        self.assertEqual(report['search_files'],self.fixture_files)
        self.assertEqual(report['images']['search-worker']['image'],report['images']['api']['image'])
        preimage = self.helper.preimage/'compose-images.json'
        frozen = json.loads(preimage.read_text())
        for name in ('searxng','search-egress'):
            frozen['services'][name] = report['images'][name]
        preimage.write_text(json.dumps(frozen))
        self.assertEqual(self.helper.manifest().returncode,0)
        manifest = json.loads((self.helper.evidence/'manifest.json').read_text())
        self.assertTrue({'api','search-worker','search-egress','searxng'} <= set(manifest['images']))

    def test_unknown_search_network_drift_rejected_before_cutover(self):
        self.helper.config['networks']['search']['internal'] = False
        self.assertNotEqual(self.helper.candidate().returncode,0)
        self.assertFalse((self.helper.evidence/'images.json').exists())
        calls = self.helper.fixture.with_suffix('.calls').read_text()
        self.assertNotIn('"up"',calls)
        self.assertNotIn('"build"',calls)


if __name__=='__main__':unittest.main()
