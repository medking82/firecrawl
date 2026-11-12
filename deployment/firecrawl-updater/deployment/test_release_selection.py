"""Select releases from a real disposable upstream, without series restrictions."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SYNC = Path(__file__).with_name('firecrawl_update.sh').resolve()


class ReleaseSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.upstream = self.root / 'upstream'
        self.repo = self.root / 'repo'
        for repo in (self.upstream, self.repo):
            repo.mkdir()
            self.git(repo, 'init', '--initial-branch=main', str(repo))
            self.git(repo, 'config', 'user.name', 'Fixture')
            self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
            self.git(repo, 'commit', '--allow-empty', '-m', 'base')
        self.git(self.repo, 'remote', 'add', 'upstream', str(self.upstream))

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def select(self):
        # A legacy environment setting must no longer restrict discovery.
        env = {**os.environ, 'FIRECRAWL_UPDATE_SERIES': 'v2.11', 'SYNC': str(SYNC)}
        return subprocess.run(['bash', '-c', 'source "$SYNC"; select_latest_stable_tag'],
                              cwd=self.repo, env=env, capture_output=True, text=True)

    def test_selects_new_minor_and_major_and_ignores_fork_only_tags(self):
        for tag in ('v2.11.999', 'v2.12.0', 'v3.0.0', 'v3.2.9', 'v3.2.10',
                    'v4.0.0-rc.1', 'v8.0.0-beta', 'v10.0.0+build', 'v03.0.0', 'nightly'):
            self.git(self.upstream, 'tag', tag)
        self.git(self.repo, 'tag', 'v99.0.0')
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'v3.2.10')

    def test_annotated_stable_tag_is_supported(self):
        self.git(self.upstream, 'tag', '-a', 'v5.0.0', '-m', 'stable')
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'v5.0.0')

    def test_no_stable_release_fails_without_local_fallback(self):
        self.git(self.upstream, 'tag', 'v4.0.0-rc.1')
        self.git(self.repo, 'tag', 'v2.11.999')
        result = self.select()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_unreachable_upstream_fails_without_local_fallback(self):
        self.git(self.repo, 'remote', 'set-url', 'upstream', str(self.root / 'missing'))
        self.git(self.repo, 'tag', 'v2.11.999')
        result = self.select()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
