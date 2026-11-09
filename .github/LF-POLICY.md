# LF policy

All tracked text in current trees must use LF. Git's own text/binary detection
(`git ls-files --eol`) defines text; binary blobs, submodules and symlink targets
are outside this content policy. Untracked and ignored files are checked when
added to the index. Editors can still write CRLF before Git checks run.

## Enable local hooks

For every clone, run `git config core.hooksPath .githooks`. The setting is local
and is not inherited by cloning. `.githooks/pre-commit` checks both the index
(including `GIT_INDEX_FILE`) and tracked working files. When API dependencies are
installed, it also runs the existing Husky knip and lint-staged checks.

API `pnpm install` runs Husky 9 prepare and may switch `core.hooksPath` to
`apps/api/.husky/_`. Both the source Husky pre-commit and pre-push hooks include
the LF checks, so either supported hook path retains the policy. API quality
checks require its installed dependencies and pnpm.

Windows Git runs the checker in WSL `Ubuntu-26.04`. Set
`FIRECRAWL_WSL_DISTRO` to use a different installed distribution containing
Python 3 and Git. Missing WSL, a missing distribution, an unreadable repository,
or an incomplete history causes a failure, not a successful check. Explicit
WSL path translation preserves alternate indexes and Windows-created linked
worktrees. Native Linux and WSL Git use Python 3 directly.

## Commit and push checks

The pre-push hook reads Git's ref updates from stdin and checks the actual
outgoing commits, including branches other than the checkout, intermediate
commits, multiple refs and annotated tags to commits. Ref deletions transfer no
commits and require no scan. Uncommitted working-tree changes do not affect a
push. Fetch missing remote objects before retrying an incomplete range.

History checking uses isolated temporary indexes and checks each distinct blob
only once per invocation. It never rewrites commits or the user's index.

The immutable policy baseline is `0feb5f379`. Earlier upstream history is
excluded. Imported upstream merge parents without this fork's LF policy are
legacy history; their merged result and all descendants of policy adoption are
checked. Outgoing tips must retain the root LF attributes. This preserves the
fork's upstream history without claiming old CRLF commits were rewritten.

## CI and protected main

The `LF line endings` workflow's job is named `LF policy`. It fetches full
history and checks the checkout, all reachable policy-era trees and the real
Git regression suite on pushes and PRs. A clean final commit cannot hide CRLF
in an earlier policy-era commit. Shallow history fails with fetch instructions.

`main` requires the GitHub Actions `LF policy` check, including administrators,
with strict status checking. Force pushes and deletion are disabled. Before
updating main, publish the candidate commit on another branch, wait for its
exact SHA to pass LF policy, then fast-forward main. Automated fork sync follows
this same path; it does not bypass protection. No PR approval is required by
this LF-only protection. The repository owner can still change protection or
workflow code; this is a Git policy, not a restriction on filesystem writes.

## Verification

Run in Linux/WSL:

```sh
python3 -B .github/scripts/test_line_endings.py -v
python3 -B .github/scripts/check-line-endings.py
python3 -B .github/scripts/check-line-endings.py --commits HEAD
```
