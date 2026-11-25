import ast
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def audit_source():
    text = (HERE / 'firecrawl_update.sh').read_text()
    marker = 'package_audit_source = '
    start = text.index(marker) + len(marker)
    return ast.literal_eval(text[start:text.index('\n', start)])


@unittest.skipUnless(shutil.which('node'), 'node required')
class PackageAuditTests(unittest.TestCase):
    def audit(self, packages):
        with tempfile.TemporaryDirectory() as root:
            for i, (name, version) in enumerate(packages):
                folder = Path(root, 'node_modules', '.pnpm', f'{name}@{version}-{i}', 'node_modules', name)
                folder.mkdir(parents=True)
                (folder / 'package.json').write_text(json.dumps({'name': name, 'version': version}))
            return subprocess.run(['node', '-e', audit_source(), root], capture_output=True, text=True, timeout=60)

    def test_dependency_removed_upstream_is_reported_empty(self):
        result = self.audit([('decode-uri-component', '0.5.1')])
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(found['systeminformation'], [])
        self.assertEqual([c['version'] for c in found['decode-uri-component']], ['0.5.1'])

    def test_vulnerable_copy_still_fails(self):
        for packages in ([('decode-uri-component', '0.5.0')],
                         [('decode-uri-component', '0.5.1'), ('systeminformation', '5.33.15')]):
            with self.subTest(packages=packages):
                result = self.audit(packages)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('vulnerable package version', result.stderr)


if __name__ == '__main__':
    unittest.main()
