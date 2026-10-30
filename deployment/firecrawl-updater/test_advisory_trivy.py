import json
import hashlib
import io
import os
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import advisory_trivy as a

CONFIG = b'fixture image data'
IMAGE = 'sha256:' + hashlib.sha256(CONFIG).hexdigest()
CONTAINER = 'c' * 64


def encoded(obj):
    return json.dumps(obj).encode()


def document(vulns=None):
    return {'SchemaVersion': 2, 'ArtifactType': 'container_image', 'Metadata': {'ImageID': IMAGE},
            'Results': [{'Target': 'app', 'Type': 'npm', 'Vulnerabilities': vulns or []}]}


def vulnerability(severity='HIGH'):
    return {'VulnerabilityID': 'CVE-123', 'PkgName': 'example', 'InstalledVersion': '1.0', 'Severity': severity}


class Fake:
    def __init__(self, doc=None):
        self.doc = document() if doc is None else doc
        self.calls = []
        self.container = CONTAINER

    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ['docker', 'container', 'inspect']:
            field = args[4]
            return encoded({'{{json .Id}}': self.container, '{{json .Image}}': IMAGE,
                            '{{json .State.Running}}': True}[field])
        if args[:3] == ['docker', 'image', 'inspect']:
            return encoded(a.TRIVY if args[-1] == a.TRIVY else IMAGE)
        if args[:3] == ['docker', 'image', 'save']:
            with tarfile.open(args[-2], 'w') as tar:
                for name, data in [('manifest.json', encoded([{'Config': 'config.json'}])), ('config.json', CONFIG)]:
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
            return b''
        if args[:2] == ['docker', 'run']:
            return encoded(self.doc)
        raise AssertionError(args)


class Tests(unittest.TestCase):
    def test_candidate_is_immutable_and_isolated(self):
        fake = Fake(document([vulnerability(), vulnerability('LOW')]))
        result = a.scan('candidate', 'mutable:tag', fake)
        self.assertEqual(result['image_id'], IMAGE)
        self.assertEqual(len(result['findings']), 1)
        save = next(c for c in fake.calls if c[:3] == ['docker', 'image', 'save'])
        self.assertEqual(save[-1], IMAGE)
        command = next(c for c in fake.calls if c[:2] == ['docker', 'run'])
        self.assertIn(a.TRIVY, command)
        self.assertIn('--pull=never', command)
        self.assertIn('--read-only', command)
        self.assertFalse(any('docker.sock' in x or 'DOCKER_CONFIG' in x for x in command))

    def test_archive_permissions_and_no_secret_output(self):
        fake = Fake(document([vulnerability()]))
        real_chmod = os.chmod
        observed = []
        def chmod(path, mode):
            observed.append((path, mode))
            real_chmod(path, mode)
        def execute(args):
            if args[:2] == ['docker', 'run']:
                path = observed[0][0]
                self.assertEqual(observed[0][1], 0o644)
                if os.name == 'posix':
                    self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
                    self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            return fake(args)
        with patch.object(a.os, 'chmod', side_effect=chmod):
            result = a.scan('candidate', 'image', execute)
        self.assertNotIn('fixture image data', json.dumps(result))

    def test_running_scanned_and_recaptured(self):
        fake = Fake()
        result = a.scan('running', 'api', fake)
        self.assertEqual(result['status'], 'no_high_critical_findings')
        self.assertEqual(sum(c[:3] == ['docker', 'container', 'inspect'] for c in fake.calls), 6)

    def test_running_replacement_fails(self):
        fake = Fake()
        def execute(args):
            output = fake(args)
            if args[:2] == ['docker', 'run']:
                fake.container = 'd' * 64
            return output
        with self.assertRaises(a.ScanError):
            a.scan('running', 'api', execute)

    def test_invalid_reports_fail_closed(self):
        cases = [b'{}', b'{"SchemaVersion":2,"SchemaVersion":2}', b'not json',
                 encoded({**document(), 'Metadata': {'ImageID': 'invalid'}}),
                 encoded({**document(), 'Results': []}),
                 encoded(document([vulnerability('UNEXPECTED')])),
                 encoded(document([{'Severity': 'HIGH'}]))]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(a.ScanError):
                a.report(case, IMAGE)

    def test_archive_config_hash_mismatch_fails(self):
        fake = Fake()
        def execute(args):
            result = fake(args)
            if args[:3] == ['docker', 'image', 'inspect'] and args[-1] != a.TRIVY:
                return encoded('sha256:' + 'b' * 64)
            return result
        with self.assertRaisesRegex(a.ScanError, 'archive image identity mismatch'):
            a.scan('candidate', 'image', execute)

    def test_trivy_archive_identity_can_differ(self):
        obj = document()
        obj['Metadata']['ImageID'] = a.TRIVY
        self.assertEqual(a.report(encoded(obj), IMAGE), [])

    def test_oci_index_links_archive_config(self):
        config_digest = IMAGE
        manifest = encoded({'config': {'digest': config_digest}})
        manifest_digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        index_blob = encoded({'manifests': [{'digest': manifest_digest}]})
        image = 'sha256:' + hashlib.sha256(index_blob).hexdigest()
        files = {'manifest.json': encoded([{'Config': 'config.json'}]), 'config.json': CONFIG,
                 'index.json': encoded({'manifests': [{'digest': image}]}),
                 'blobs/sha256/' + image[7:]: index_blob,
                 'blobs/sha256/' + manifest_digest[7:]: manifest}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'image.tar')
            with tarfile.open(path, 'w') as tar:
                for name, data in files.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
            a.verify_archive(path, image)
            with self.assertRaises(a.ScanError):
                a.verify_archive(path, 'sha256:' + 'f' * 64)

    def test_subprocess_error_never_exposes_stderr(self):
        failed = subprocess.CompletedProcess([], 1, b'', b'secret-token')
        with patch.object(a.subprocess, 'run', return_value=failed):
            with self.assertRaises(a.ScanError) as cm:
                a.run(['docker', 'image', 'inspect'])
        self.assertNotIn('secret-token', str(cm.exception))
        with patch.object(a.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 1)):
            with self.assertRaises(a.ScanError):
                a.run(['docker'])


if __name__ == '__main__':
    unittest.main()
