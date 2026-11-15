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


if __name__=='__main__':unittest.main()
