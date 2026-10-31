# Standalone Firecrawl updater

GA401 owns this updater independently of other applications. Since 2026-10-31 it
is the approval-gated core upgrade owner, installed as `~/.local/bin/firecrawl-upgrade`
and run daily by `firecrawl-upgrade-candidate.timer`. `firecrawl-update` and
`firecrawl-update.timer` stay with the image-only security refresh
(`../self-hosted-auto/security_refresh.py`). Install a reviewed revision with
`install_firecrawl_update.py`, the exact installed preimage hash (`absent` for the
first installation) and the two unit files; enabling the timer is a separate step.
The installer keeps a private preimage and coordinates with the updater lock.

Each new upstream candidate is built from its source commit in an isolated
worktree, patched using integrity-checked package inputs, and audited before
cutover. The transport guard accepts only complete previously reviewed profiles;
unknown transport changes stop for review. The updater verifies that all exact
rollback images exist and preserves the running PostgreSQL and Playwright images.
The timer updates the core API only; monitor and browser overlays have separate
owners and require their own compatibility review and deployment.

## Approval gate

The Owner decided on 2026-10-31 that candidates are built automatically and cut
over only after the Owner approves that exact tag.

1. A scheduled run selects the newest `v2.11.*` tag, prepares and admits its
   candidate under `run.lock` and a shared `update.lock`, then writes
   `candidate-ready.json` (tag, commit, image IDs, checks hash, evidence path) and
   stops. It never takes the exclusive lock or switches source.
2. `firecrawl-upgrade --approve vX.Y.Z` writes `approval.json`, bound to the tag,
   commit, evidence path, checks hash, API image and the receipt's own hash.
   `firecrawl-upgrade --status` shows the receipt, approval, notice and any blocked tag.
3. The next run cuts over only if the approval still matches the admitted
   candidate and the runtime configuration still hashes to the admitted value.
   It consumes the approval before switching source; a failed cutover rolls back
   and needs a new candidate and a new approval. Active page reads defer the
   cutover and keep the approval.
4. A newer tag supersedes the old receipt and approval. A changed runtime
   configuration withdraws the approval and rebuilds the candidate. A tag that
   failed admission is not rebuilt until its guard, `.env` or Compose override changes.

After a successful cutover the updater releases both locks and runs one security
refresh, so the new API image receives current Debian security packages. The
security refresh failing does not roll back the core; the notice says so.

Rollback snapshots accept the image-only overlays that earlier owners left in the
API container's Compose `config_files` (updater `candidate.*`, `brotli-promotion-*`
and security-refresh `run-*/firecrawl-{candidate,recovery}.json`) and re-pin every
service image. Cutover renders only the base files plus the candidate image
overlay, so later security refreshes start from three files again.

## Notice

The updater opens or updates one issue, `[Firecrawl Upgrade] GA401 core candidate`,
in `medking82/pc-maintenance`, assigned to `medking82`, through `gh api` directly
(Actions can be blocked by billing). It carries the tag, commit, upstream compare
link, admission and live smoke results and the approve command. Cutover success
closes it; failure, rollback or a blocked admission comments. Delivery failures are
retried by the next run and never block the candidate. Issues opened with the
Owner's own token do not trigger a GitHub email; the issue list is the notice.

## Retention

Candidates are recorded under `candidates/`. Superseded, stale, failed and blocked
candidates lose their patched and base images and their `update-<commit>`
worktree once no container, security journal, recovery preimage, receipt or
approval references them. Evidence directories are kept: running containers and
security journals reference their `images.json`. Earlier untracked candidates are
not touched.

## Advisories and checks

The package policy covers the listed advisory ranges, not an automatic feed of
all new vulnerabilities. Reassess advisories and patch inputs for new incidents.
The current inputs cover [CVE-2026-118664](https://github.com/advisories/GHSA-qr28-fpjm-wq7f)
and [CVE-2026-116576](https://github.com/advisories/GHSA-3f7j-7jwc-fxfj).

On GA401, run the isolated tests from this directory with
`python3 -B -m unittest deployment.test_firecrawl_approval deployment.test_firecrawl_candidate deployment.test_firecrawl_patch deployment.test_research_update deployment.test_fork_sync`
and check shell syntax with `bash -n deployment/firecrawl_update.sh`. After an
installation, `firecrawl-upgrade --check-only` exercises the installed guard plus
real search and scrape without initiating an update.
