import unittest

from recovery_volume import postgres_volume, verify_same_volume


def container(name='volume-a', kind='volume', writable=True):
    return {'Mounts': [{'Destination': '/var/lib/postgresql/data', 'Name': name,
                        'Type': kind, 'RW': writable}]}


class VolumeIdentityTest(unittest.TestCase):
    def test_exact_identity_is_preserved(self):
        self.assertEqual(verify_same_volume(container(), container()), 'volume-a')

    def test_different_volume_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_same_volume(container(), container('volume-b'))

    def test_missing_duplicate_bind_and_readonly_are_rejected(self):
        cases = [{}, {'Mounts': []}, {'Mounts': container()['Mounts'] * 2},
                 container(kind='bind'), container(name=''), container(writable=False)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                postgres_volume(case)


if __name__ == '__main__':
    unittest.main()
