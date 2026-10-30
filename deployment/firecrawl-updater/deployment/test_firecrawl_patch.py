"""Exercise the patcher actually embedded in the single-file updater."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / 'firecrawl_update.sh').read_text(encoding='utf-8')

def embedded(marker):
    return SOURCE.split("<<'" + marker + "'\n", 1)[1].split('\n' + marker, 1)[0] + '\n'

class EmbeddedPatchTests(unittest.TestCase):
    def test_reviewed_inputs_and_immutable_build_contract(self):
        spec = json.loads(embedded('PATCH_SPEC'))
        self.assertEqual([(x['name'], x['target']) for x in spec],
                         [('decode-uri-component', '0.5.1'), ('systeminformation', '5.33.18')])
        lock = embedded('PATCH_LOCK')
        self.assertIn('sha512-vWHQkXgg2cE3BB4rR0c+PYw9BCQzLoNIRMI7AunIkZst74tw/vpPp7VLoYtsR/cy1f55g3XDWUslfeCZjiZJyA==', lock)
        self.assertIn('sha512-SF8DBQx3ypa8zDqZlezpHQ6M2g7TrqSEWpAtC4OsHCdLmi7J2WdxaSbA8/DJyEn2l/CHPrW0VFenoqoDccrw6g==', lock)
        self.assertIn('FROM ${BASE_API}', embedded('PATCH_DOCKERFILE'))
        self.assertIn('firecrawl-updater-base:', embedded('PATCH_SCRIPT'))
        self.assertIn("patched['RootFS']['Layers'][:len(layers)] != layers", SOURCE)
        self.assertIn('build --build-arg "GIT_SHA=$latest_commit"', SOURCE)
        self.assertIn("image = patched_image if name == 'api'", SOURCE)
        self.assertIn("env.get('FIRECRAWL_BUILD_SHA') != commit", SOURCE)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_embedded_patcher_fixtures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'patch.cjs').write_text(embedded('PATCH_SCRIPT'), encoding='utf-8')
            shutil.copyfile(ROOT / 'test_firecrawl_patch_fixture.cjs', root / 'patch.test.cjs')
            result = subprocess.run(['node', '--test', str(root / 'patch.test.cjs')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__':
    unittest.main()
