import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from recovery_backup import FILES, verify_backup


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        if os.name == 'nt':
            original_lstat = Path.lstat
            def private_mode(path, *args, **kwargs):
                result = list(original_lstat(path, *args, **kwargs))
                result[0] &= ~0o077
                return os.stat_result(result)
            patch = mock.patch.object(Path, 'lstat', private_mode)
            patch.start()
            self.addCleanup(patch.stop)
        self.backup = self.root / 'db-backup.fixture'
        self.backup.mkdir(mode=0o700)
        rows = []
        for name in sorted(FILES):
            data = b'PGDMPfixture' if name.endswith('.dump') else b'fixture globals'
            file = self.backup / name
            file.write_bytes(data)
            os.chmod(file, 0o600)
            rows.append(hashlib.sha256(data).hexdigest() + '  ' + str(file))
        self.manifest = self.backup / 'SHA256SUMS'
        self.manifest.write_text('\n'.join(rows) + '\n')
        os.chmod(self.manifest, 0o600)

    def check(self, **kwargs):
        kwargs.setdefault('uid', self.backup.stat().st_uid)
        return verify_backup(self.backup, 3600, root=self.root, **kwargs)

    def test_valid_and_self_entry_rejected(self):
        self.assertEqual(self.check()['files_verified'], 8)
        with self.manifest.open('a') as f: f.write('0' * 64 + '  ' + str(self.manifest) + '\n')
        with self.assertRaises(ValueError): self.check()

    def test_tamper_missing_and_stale_refuse(self):
        target = self.backup / sorted(FILES)[0]
        original = target.read_bytes()
        target.write_bytes(b'changed')
        with self.assertRaises(ValueError): self.check()
        target.write_bytes(original)
        with self.assertRaises(ValueError): self.check(now=time.time() + 7200)
        target.unlink()
        with self.assertRaises((ValueError, OSError)): self.check()

    def test_wrong_root_duplicate_and_wrong_owner_refuse(self):
        with self.assertRaises(ValueError): verify_backup(self.backup, 3600, root=self.backup, uid=self.backup.stat().st_uid)
        with self.assertRaises(ValueError): self.check(uid=self.backup.stat().st_uid + 1)
        self.manifest.write_text(self.manifest.read_text() * 2)
        with self.assertRaises(ValueError): self.check()


if __name__ == '__main__':
    unittest.main()
