"""Check the index/worktree, or every outgoing commit, for CRLF text."""
import argparse
import os
import subprocess
import sys
import tempfile

# Immutable upstream history predates this fork's LF policy.
POLICY_BASE = "0feb5f379a2657623a3acc5ca7108a22dcf7c852"


def git(*args, check=True):
    return subprocess.run(["git", *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def checkout_check():
    failures = []
    for row in git("ls-files", "--eol", "-z").stdout.split(b"\0"):
        if not row:
            continue
        metadata, path = row.split(b"\t", 1)
        if any(s in (b"i/crlf", b"i/mixed", b"w/crlf", b"w/mixed")
               for s in metadata.split()[:2]):
            failures.append((os.fsdecode(path), metadata.decode().strip()))
    for path, metadata in failures:
        print(f"CRLF/mixed text: {path} ({metadata})", file=sys.stderr)
    if failures:
        print("Save these files with LF and stage them again.", file=sys.stderr)
    return bool(failures)


def policy_present(commit):
    attrs = git("show", f"{commit}:.gitattributes", check=False)
    return attrs.returncode == 0 and any(
        line.split()[:3] == [b"*", b"text=auto", b"eol=lf"]
        for line in attrs.stdout.splitlines())


def history_check(tips, excludes):
    """Check committed bytes, independent of checkout and staged index."""
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
        raise ValueError("LF history check needs full history; fetch --unshallow first.")
    baseline = git("rev-parse", "--verify", f"{POLICY_BASE}^{{commit}}", check=False)
    if baseline.returncode == 0:
        excludes = [*excludes, *git("rev-parse", f"{POLICY_BASE}^").stdout.decode().split()]
    resolved = []
    for tip in tips:
        commit = git("rev-parse", "--verify", f"{tip}^{{commit}}").stdout.decode().strip()
        if not policy_present(commit):
            raise ValueError(f"Outgoing tip {tip} must retain the root LF policy.")
        resolved.append(commit)
    commits = git("rev-list", *resolved, *(f"^{ref}" for ref in excludes), "--").stdout.decode().splitlines()
    failed = False
    checked_trees = set()
    blob_states = {}
    for commit in commits:
        # Legacy upstream parents lack this policy; descendants of adoption
        # remain subject to checking even if they remove attributes.
        descendant = baseline.returncode == 0 and git(
            "merge-base", "--is-ancestor", POLICY_BASE, commit, check=False).returncode == 0
        if not descendant and not policy_present(commit):
            continue
        tree = git("rev-parse", f"{commit}^{{tree}}").stdout.strip()
        if tree in checked_trees:
            continue
        checked_trees.add(tree)
        # Classify each unique blob once with Git's own EOL detection. A text-like
        # PDF can fool grep -I; content IDs let us reuse the exact Git result.
        entries = {}
        pending = {}
        for row in git("ls-tree", "-r", "-z", commit).stdout.split(b"\0"):
            if not row:
                continue
            metadata, path = row.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if kind != b"blob" or mode == b"120000":
                continue
            entries[path] = oid
            if oid not in blob_states:
                pending.setdefault(oid, path)
        if pending:
            with tempfile.TemporaryDirectory(prefix="lf-tree-") as directory:
                env = dict(os.environ, GIT_INDEX_FILE=os.path.join(directory, "index"),
                           GIT_WORK_TREE=directory)
                subprocess.run(["git", "read-tree", commit], env=env, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                paths = list(pending.values())
                for start in range(0, len(paths), 100):
                    result = subprocess.run(
                        ["git", "--literal-pathspecs", "ls-files", "--eol", "-z", "--",
                         *(os.fsdecode(p) for p in paths[start:start + 100])],
                        env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for row in result.stdout.split(b"\0"):
                        if row:
                            metadata, path = row.split(b"\t", 1)
                            blob_states[entries[path]] = metadata.split()[0] in (b"i/crlf", b"i/mixed")
        for path, oid in entries.items():
            if blob_states[oid]:
                print(f"CRLF/mixed text in commit: {commit}:{os.fsdecode(path)}", file=sys.stderr)
                failed = True
    print(f"LF history check: {len(checked_trees)} distinct policy-era trees checked.")
    return failed


def pre_push():
    tips, excludes = [], []
    for line in sys.stdin:
        local_ref, local_oid, remote_ref, remote_oid = line.split()
        if set(local_oid) == {"0"}:
            continue
        tips.append(local_oid)
        if set(remote_oid) != {"0"}:
            git("cat-file", "-e", f"{remote_oid}^{{commit}}")
            excludes.append(remote_oid)
    return history_check(tips, excludes) if tips else False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commits", nargs="+")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--pre-push", action="store_true")
    args = parser.parse_args()
    try:
        failed = (pre_push() if args.pre_push else
                  history_check(args.commits, args.exclude) if args.commits else checkout_check())
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"LF check could not complete: {exc}", file=sys.stderr)
        return 2
    if not failed:
        print("LF check passed.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
