"""Real Git regressions for the repository's LF policy; no external services."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

POLICY = Path(__file__).resolve().parents[2]
CHECKER = ".github/scripts/check-line-endings.py"


class LFPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="firecrawl-lf-")
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="LF test", GIT_AUTHOR_EMAIL="lf@example.invalid",
                        GIT_COMMITTER_NAME="LF test", GIT_COMMITTER_EMAIL="lf@example.invalid")
        self.env.pop("GIT_INDEX_FILE", None)
        self.remotes = 0
        self.root = self.new_repo("repo", b"* text=auto eol=lf\n")
        self.git("config", "core.hooksPath", ".githooks")

    def new_repo(self, name, attributes):
        root = Path(self.temp.name) / name
        root.mkdir()
        self.git("init", "-q", "-b", "main", cwd=root)
        self.git("config", "core.autocrlf", "false", cwd=root)
        for folder in (".githooks", ".github/scripts"):
            shutil.copytree(POLICY / folder, root / folder)
        (root / ".gitattributes").write_bytes(attributes)
        (root / "sample.py").write_bytes(b"print(1)\n")
        self.git("add", ".", cwd=root)
        self.git("commit", "-qm", "baseline", cwd=root)
        return root

    def git(self, *args, check=True, cwd=None, input=None, env=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, env=env or self.env,
                              input=input, capture_output=True, check=check)

    def checker(self, *args, env=None):
        return subprocess.run([sys.executable, str(self.root / CHECKER), *args],
                              cwd=self.root, env=env or self.env, capture_output=True)

    def raw_blob(self, data, cwd=None):
        return self.git("hash-object", "-w", "--stdin", input=data, cwd=cwd).stdout.decode().strip()

    def commit_raw(self, path, data, message, cwd=None):
        """Commit bytes exactly as given, bypassing Git normalization and hooks."""
        blob = self.raw_blob(data, cwd)
        self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", cwd=cwd)
        self.git("commit", "-q", "--no-verify", "-m", message, cwd=cwd)

    def add_remote(self, cwd=None):
        self.remotes += 1
        remote = Path(self.temp.name) / f"remote-{self.remotes}.git"
        self.git("init", "--bare", "-q", str(remote))
        self.git("remote", "add", "origin", str(remote), cwd=cwd)
        return remote

    # --- pre-commit -------------------------------------------------------

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

    def test_raw_crlf_index_blob_is_rejected(self):
        blob = self.raw_blob(b"print(1)\r\n")
        self.git("update-index", "--cacheinfo", f"100644,{blob},sample.py")
        result = self.checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"i/crlf", result.stderr)

    def test_alternate_index_file_is_checked(self):
        alt = Path(self.temp.name) / "alt-index"
        env = dict(self.env, GIT_INDEX_FILE=str(alt))
        self.git("read-tree", "HEAD", env=env)
        blob = self.raw_blob(b"print(1)\r\n")
        self.git("update-index", "--cacheinfo", f"100644,{blob},sample.py", env=env)
        self.assertEqual(self.checker().returncode, 0, "default index is clean")
        result = self.checker(env=env)
        self.assertEqual(result.returncode, 1, "alternate index with CRLF passed")
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
        self.assertEqual(self.checker("--commits", "HEAD").returncode, 0)

    # --- pre-push: outgoing commits, not the checkout -------------------------

    def test_pre_push_accepts_lf_commits(self):
        self.add_remote()
        (self.root / "sample.py").write_bytes(b"print(2)\n")
        self.git("add", "sample.py")
        self.git("commit", "-qm", "next")
        self.git("push", "-q", "origin", "HEAD:main")

    def test_pre_push_rejects_crlf_on_branch_that_is_not_checked_out(self):
        self.add_remote()
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "-b", "side")
        self.commit_raw("side.py", b"x = 1\r\n", "crlf on side")
        self.git("checkout", "-q", "main")
        result = self.git("push", "origin", "side", check=False)
        self.assertNotEqual(result.returncode, 0, "CRLF branch was pushed from a clean checkout")
        self.assertIn(b"side.py", result.stderr)

    def test_pre_push_rejects_intermediate_crlf_commit_fixed_later(self):
        self.add_remote()
        self.git("push", "-q", "origin", "main")
        self.commit_raw("mid.py", b"x = 1\r\n", "crlf")
        self.commit_raw("mid.py", b"x = 1\n", "fix")
        result = self.git("push", "origin", "main", check=False)
        self.assertNotEqual(result.returncode, 0, "CRLF history was pushed behind a clean tip")
        self.assertIn(b"mid.py", result.stderr)

    def test_pre_push_ignores_history_before_lf_policy(self):
        legacy = self.new_repo("legacy", b"* text=auto\n")
        self.commit_raw("old.py", b"x = 1\r\n", "legacy crlf", cwd=legacy)
        (legacy / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        self.git("add", ".gitattributes", cwd=legacy)
        self.commit_raw("old.py", b"x = 1\n", "adopt LF policy", cwd=legacy)
        self.git("config", "core.hooksPath", ".githooks", cwd=legacy)
        self.add_remote(cwd=legacy)
        self.git("push", "-q", "origin", "main", cwd=legacy)

    # --- CI history mode ---------------------------------------------------

    def test_commit_history_mode_rejects_crlf_hidden_behind_clean_tip(self):
        self.assertEqual(self.checker("--commits", "HEAD").returncode, 0)
        self.commit_raw("hidden.py", b"x = 1\r\n", "crlf")
        self.commit_raw("hidden.py", b"x = 1\n", "fix")
        self.assertEqual(self.checker().returncode, 0, "tip checkout is clean")
        result = self.checker("--commits", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"hidden.py", result.stderr)

    # --- husky coexistence ---------------------------------------------------

    def test_husky_hooks_path_still_rejects_crlf_commit_and_push(self):
        shutil.copytree(POLICY / "apps/api/.husky", self.root / "apps/api/.husky")
        self.git("config", "core.hooksPath", "apps/api/.husky")
        (self.root / "sample.py").write_bytes(b"print(2)\r\n")
        self.git("add", "sample.py")
        result = self.git("commit", "-qm", "must fail", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"CRLF/mixed text: sample.py", result.stderr)
        self.git("reset", "-q", "--hard")
        self.add_remote()
        self.commit_raw("p.py", b"x = 1\r\n", "crlf")
        result = self.git("push", "origin", "main", check=False)
        self.assertNotEqual(result.returncode, 0, "husky hooksPath let CRLF push through")
        self.assertIn(b"p.py", result.stderr)

    def test_githooks_pre_commit_chains_husky_once_when_installed(self):
        husky = self.root / "apps/api/.husky"
        husky.mkdir(parents=True)
        (self.root / "apps/api/node_modules").mkdir()
        marker = Path(self.temp.name) / "husky-ran"
        (husky / "pre-commit").write_text(
            '[ -n "${FIRECRAWL_LF_CHECKED:-}" ] || echo double-check >&2\n'
            f'echo ran >> "{marker}"\n')
        (self.root / "sample.py").write_bytes(b"print(2)\n")
        self.git("add", "sample.py")
        result = self.git("commit", "-qm", "chained")
        self.assertEqual(marker.read_text(), "ran\n")
        self.assertNotIn(b"double-check", result.stderr)

    def test_history_preserves_default_and_alternate_indexes(self):
        default = self.root / ".git/index"
        before = default.read_bytes()
        alt = Path(self.temp.name) / "alternate-index"
        env = dict(self.env, GIT_INDEX_FILE=str(alt))
        self.git("read-tree", "HEAD", env=env)
        alternate_before = alt.read_bytes()
        self.assertEqual(self.checker("--commits", "HEAD", env=env).returncode, 0)
        self.assertEqual(default.read_bytes(), before)
        self.assertEqual(alt.read_bytes(), alternate_before)

    def test_history_handles_literal_pathspec_characters_and_tabs(self):
        self.commit_raw("odd [1]\tfile.py", b"bad\r\n", "odd path")
        result = self.checker("--commits", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"odd [1]\tfile.py", result.stderr)

    def test_pre_push_deletion_does_not_scan_unrelated_worktree(self):
        self.add_remote()
        self.git("push", "-q", "origin", "main:disposable")
        (self.root / "sample.py").write_bytes(b"print(1)\r\n")
        self.git("push", "-q", "origin", ":disposable")

    def test_pre_push_accepts_annotated_tag_to_clean_commit(self):
        self.add_remote()
        self.git("tag", "-am", "clean tag", "lf-test")
        self.git("push", "-q", "origin", "lf-test")

    def test_history_rejects_policy_removal_at_tip(self):
        (self.root / ".gitattributes").unlink()
        self.git("add", ".gitattributes")
        self.git("commit", "--no-verify", "-qm", "remove policy")
        result = self.checker("--commits", "HEAD")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"retain the root LF policy", result.stderr)

    def test_history_fails_closed_for_shallow_clone(self):
        clone = Path(self.temp.name) / "shallow"
        self.git("clone", "--depth=1", self.root.as_uri(), str(clone))
        result = subprocess.run([sys.executable, str(clone / CHECKER), "--commits", "HEAD"],
                                cwd=clone, env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"fetch --unshallow", result.stderr)

    def test_pre_push_rejects_multiple_refs_when_one_has_crlf(self):
        self.add_remote()
        self.git("checkout", "-q", "-b", "bad")
        self.commit_raw("bad.py", b"bad\r\n", "bad ref")
        self.git("checkout", "-q", "main")
        result = self.git("push", "origin", "main", "bad", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"bad.py", result.stderr)

    # --- checkout / clone -----------------------------------------------------

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
