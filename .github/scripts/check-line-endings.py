"""Reject CRLF/mixed line endings in Git-tracked text, including staged blobs."""
import subprocess
import sys


def main():
    result = subprocess.run(
        ["git", "ls-files", "--eol", "-z"], check=True, stdout=subprocess.PIPE
    )
    failures = []
    for row in result.stdout.split(b"\0"):
        if not row:
            continue
        metadata, path = row.split(b"\t", 1)
        states = metadata.split()
        if any(state in (b"i/crlf", b"i/mixed", b"w/crlf", b"w/mixed") for state in states[:2]):
            failures.append((path.decode("utf-8", errors="replace"), metadata.decode()))
    for path, metadata in failures:
        print(f"CRLF/mixed text: {path} ({metadata.strip()})", file=sys.stderr)
    if failures:
        print("Save these files with LF and stage them again.", file=sys.stderr)
        return 1
    print("LF check passed: Git index and tracked working-tree text are clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
