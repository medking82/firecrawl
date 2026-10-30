import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

base = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('upgrade_v626', base / 'upgrade-v626.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class UpgradePreflightTests(unittest.TestCase):
    def test_exact_current_and_base_layers_required(self):
        old = {'Image': u.CURRENT_IMAGE}; core = {'Image': u.BASE_IMAGE}
        images = {u.BASE_IMAGE: {'RootFS': {'Layers': ['a', 'b']}},
                  u.EXPECTED: {'RootFS': {'Layers': ['a', 'b', 'c']}}}
        with mock.patch.object(u.p, 'inspect', side_effect=lambda kind, ref: images[ref]):
            u.verify_image_binding(old, core, u.EXPECTED)
            for bad_old, bad_base in [({'Image': 'wrong'}, core), (old, {'Image': 'wrong'})]:
                with self.assertRaises(u.p.Stop): u.verify_image_binding(bad_old, bad_base, u.EXPECTED)
            images[u.EXPECTED]['RootFS']['Layers'] = ['a', 'different', 'c']
            with self.assertRaises(u.p.Stop): u.verify_image_binding(old, core, u.EXPECTED)

    def test_idle_rejects_busy_queues_checks_or_database(self):
        for values in (['1'], ['0', '1'], ['0', '0', '1']):
            with self.subTest(values=values), mock.patch.object(u, 'sql', side_effect=values):
                self.assertFalse(u.idle('db'))
        with mock.patch.object(u, 'sql', side_effect=['0', '0', '0']):
            self.assertTrue(u.idle('db'))

    def test_http_connections_are_not_treated_as_drained(self):
        with mock.patch.object(u.p, 'run', return_value='ESTAB 0 0'):
            self.assertFalse(u.http_idle())
        with mock.patch.object(u.p, 'run', return_value=''):
            self.assertTrue(u.http_idle())

    def test_image_only_env_update_preserves_secret_bytes(self):
        old = b'SECRET=unchanged=a\r\nMONITOR_IMAGE=old\nBASE_API=base\nOTHER=unchanged\n'
        new = u.p.replace_env(old, {'MONITOR_IMAGE': u.EXPECTED, 'BASE_API': u.BASE_IMAGE})
        self.assertEqual(new.replace(u.EXPECTED.encode(), b'old').replace(u.BASE_IMAGE.encode(), b'base'), old)

    def test_reviewed_build_env_and_patch_labels_only(self):
        old_labels = {'org.firecrawl.patch.base': 'base', 'org.firecrawl.patch.base-image-id': 'image'}
        old_image = {'Config': {'Env': ['FIRECRAWL_BUILD_SHA=unknown', 'OTHER=x'], 'Labels': old_labels}}
        new_image = {'Config': {'Env': ['FIRECRAWL_BUILD_SHA=cccc2a6e554be566e1a504def286fca281b69024', 'OTHER=x'], 'Labels': {}}}
        before = {'Config': {'Env': old_image['Config']['Env'][:], 'Labels': dict(old_labels)}, 'HostConfig': {}, 'Mounts': []}
        after = {'Config': {'Env': new_image['Config']['Env'][:], 'Labels': {}}, 'HostConfig': {}, 'Mounts': []}
        with mock.patch.object(u.p, 'inspect', side_effect=lambda kind, ref: old_image if ref == u.CURRENT_IMAGE else new_image):
            u.verify_api_configuration(before, after)
            after['Config']['Env'].append('UNEXPECTED=x')
            with self.assertRaises(u.p.Stop): u.verify_api_configuration(before, after)
            after['Config']['Env'].pop()
            new_image['Config']['Env'].append('OTHER_NEW=x')
            with self.assertRaises(u.p.Stop): u.verify_api_configuration(before, after)


if __name__ == '__main__':
    unittest.main()
