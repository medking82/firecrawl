"""Real Git regressions for the repository's LF policy; no external services."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

POLICY = Path(__file__).resolve().parents[2]


class LFPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="firecrawl-lf-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="LF test", GIT_AUTHOR_EMAIL="lf@example.invalid",
                        GIT_COMMITTER_NAME="LF test", GIT_COMMITTER_EMAIL="lf@example.invalid")
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        for folder in (".githooks", ".github/scripts"):
            target = self.root / folder
            if (POLICY / folder).exists():
                shutil.copytree(POLICY / folder, target)
        (self.root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        (self.root / "sample.py").write_bytes(b"print(1)\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.git("config", "core.hooksPath", ".githooks")

    def git(self, *args, check=True, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, env=self.env,
                              capture_output=True, check=check)

    def checker(self):
        return subprocess.run([sys.executable, str(POLICY / ".github/scripts/check-line-endings.py")],
                              cwd=self.root, capture_output=True)

    def test_real_commit_rejects_crlf_worktree_after_git_normalizes_index(self):
        (self.root / "sample.py").write_bytes(b"print(2)\r\n")
        self.git("add", "sample.py")
        self.assertEqual(self.git("show", ":sample.py").stdout, b"print(2)\n")
        result = self.git("commit", "-qm", "must fail", check=False)
        self.assertNotEqual(result.returncode, 0, "CRLF worktree was committed")
        self.assertIn(b"CRLF/mixed text: sample.py", result.stderr)
        (self.root / "sample.py").write_bytes(b"print(2)\n")
        self.git("add", "sample.py")
        self.git("commit", "-qm", "fixed")


    def test_pre_push_rejects_crlf_and_accepts_lf(self):
        remote = Path(self.temp.name) / "remote.git"
        self.git("init", "--bare", "-q", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "HEAD:main")
        (self.root / "sample.py").write_bytes(b"print(2)\n")
        self.git("add", "sample.py")
        self.git("commit", "-qm", "next")
        (self.root / "sample.py").write_bytes(b"print(2)\r\n")
        result = self.git("push", "origin", "HEAD:main", check=False)
        self.assertNotEqual(result.returncode, 0, "CRLF worktree was pushed")
        self.assertIn(b"CRLF/mixed text: sample.py", result.stderr)
        (self.root / "sample.py").write_bytes(b"print(2)\n")
        self.git("push", "-q", "origin", "HEAD:main")

    def test_raw_crlf_index_blob_is_rejected(self):
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                              input=b"print(1)\r\n", cwd=self.root, env=self.env,
                              capture_output=True, check=True).stdout.decode().strip()
        self.git("update-index", "--cacheinfo", "100644", blob, "sample.py")
        result = self.checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"i/crlf", result.stderr)

    def test_mixed_worktree_is_rejected(self):
        (self.root / "sample.py").write_bytes(b"print(1)\nprint(2)\r\n")
        result = self.checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"w/mixed", result.stderr)

    def test_lf_and_binary_are_accepted(self):
        (self.root / "binary.dat").write_bytes(b"\x00\r\n\xff")
        self.git("add", "binary.dat")
        self.assertEqual(self.checker().returncode, 0)
        self.git("commit", "-qm", "binary is unchanged")
        self.assertEqual(self.git("show", "HEAD:binary.dat").stdout, b"\x00\r\n\xff")

    def test_checkout_overrides_autocrlf_true(self):
        (self.root / "sample.py").unlink()
        self.git("-c", "core.autocrlf=true", "checkout-index", "--force", "--", "sample.py")
        self.assertEqual((self.root / "sample.py").read_bytes(), b"print(1)\n")

    def test_fresh_clone_overrides_autocrlf_true(self):
        clone = Path(self.temp.name) / "clone"
        self.git("-c", "core.autocrlf=true", "clone", "-q", str(self.root), str(clone))
        self.assertEqual((clone / "sample.py").read_bytes(), b"print(1)\n")


if __name__ == "__main__":
    unittest.main()
