# Standalone Firecrawl updater

GA401 owns this updater independently of other applications. It is installed as
`~/.local/bin/firecrawl-upgrade` and run by the systemd user timer
`firecrawl-upgrade-candidate.timer`, independently of Hermes or an interactive
session. The reviewed service enables `FIRECRAWL_AUTO_DEPLOY=1`: admitted
candidates deploy automatically without per-release human approval.
`firecrawl-update` and `firecrawl-update.timer` stay with the image-only security refresh
(`../self-hosted-auto/security_refresh.py`). Install a reviewed revision with
`install_firecrawl_update.py`, the exact installed preimage hash (`absent` for the
first installation) and the two unit files; enabling the timer is a separate step.
The installer keeps a private preimage and coordinates with the updater lock.

Each new upstream candidate is built from its source commit in an isolated
worktree, patched using integrity-checked package inputs, and audited before
cutover. The transport guard accepts complete audited profiles by exact bytes
or an equivalent parsed JavaScript AST. Comments, formatting and source positions
do not block a compatible upgrade; executable logic, ASI, directive, regexp and
tagged-template changes still require qualification. The latest profile also
binds the hosted-shell entry points and just-bash's resolved runtime dependency
graph, including external packages. A candidate containing a hosted shell cannot
reuse a legacy profile that lacks that boundary.
Dependency versions, Undici's full library tree and runtime configuration remain
required. New transport logic earns a new checked-in audit record after review
and PC WSL verification, then the system task deploys without a human approval
step. A changed installed guard invalidates an older blocked-candidate key, so
it is retried automatically. The updater verifies that all exact
rollback images exist and preserves the running PostgreSQL and Playwright images.
The timer updates the core API only; monitor and browser overlays have separate
owners and require their own compatibility review and deployment.

## Automatic promotion and retained authorization receipts

The systemd service checks at 04:30 and 05:30 local time, each with up to 30
minutes of randomized delay. The second slot retries lock contention or temporary
health/build failures without repeatedly building throughout the day.
`Persistent=true` catches a missed calendar run after the host resumes. GA401's
user manager has lingering enabled so logout does not stop the timer.

1. Each run selects the highest stable `vMAJOR.MINOR.PATCH` tag directly from
   upstream across all major/minor versions. Prerelease and fork-only tags are
   excluded. It builds and admits a candidate under `run.lock` and a shared
   `update.lock`, then records the exact commit, images and checks in
   `candidate-ready.json`.
2. In automatic mode, successful live health checks and unchanged runtime
   configuration authorize that exact receipt as `systemd-auto`. The same run
   revalidates the binding and enters the existing cutover path. A newly built
   receipt reuses that run's successful live check; an existing receipt requires
   a fresh live check. Unknown transport, failed package checks, missing rollback
   images and other failed admission checks never earn automatic authorization.
3. Cutover acquires the exclusive transport lock, snapshots exact service images
   and consumes the single-use authorization before switching source. Active
   page reads defer the cutover and keep the receipt for the next scheduled run.
   Post-cutover transport, search and scrape smoke tests must pass. Failure rolls
   back to the prior source and exact images. TERM/INT also invoke recovery;
   the service allows 15 minutes for shutdown recovery.
4. An automatic cutover failure also blocks the same tag/commit/config/guard
   combination, preventing an outage loop at every timer tick. A newer release
   or changed configuration/guard is evaluated afresh. Build failures and live
   health failures can retry at the next scheduled run. Changed configuration
   invalidates prior authorization and rebuilds the candidate first.
5. A newer tag supersedes the old receipt and authorization. Evidence is retained
   for diagnosis and rollback. `firecrawl-upgrade --status` shows candidate,
   authorization, blocked and notice state.

Direct CLI invocations default to `FIRECRAWL_AUTO_DEPLOY=0` (only `0` or `1` is
accepted), preserving the optional manual `--approve vX.Y.Z` workflow. Turning
automatic mode off withdraws leftover `systemd-auto` authorizations before
considering cutover. To disable automation for the system task, override its
`Environment=FIRECRAWL_AUTO_DEPLOY=0` and reload the user manager. Installing new
units requires `systemctl --user daemon-reload` and restarting the timer; verify
the effective service environment and both calendar slots, including drop-ins.

After successful cutover, the updater releases its locks and runs one security
refresh, so the new API image receives current Debian security packages. Failure
of that refresh is reported separately and does not roll back the core.


Rollback snapshots accept the image-only overlays that earlier owners left in the
API container's Compose `config_files` (updater `candidate.*`, `brotli-promotion-*`
and security-refresh `run-*/firecrawl-{candidate,recovery}.json`) and re-pin every
service image. Cutover renders only the base files plus the candidate image
overlay, so later security refreshes start from three files again.

## Notice

The updater opens or updates one issue, `[Firecrawl Upgrade] GA401 core candidate`,
in `medking82/pc-maintenance`, assigned to `medking82`, through `gh api` directly
(Actions can be blocked by billing). It carries the tag, commit, upstream compare
link, admission and live smoke results. Automatic mode only reports exceptional
conditions; a successful automatic run never asks for approval. Manual mode also
includes the optional approve command. Cutover success closes the notice; failure,
rollback or blocked admission updates it. Delivery failures are
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

Run the isolated tests in PC WSL from this directory:
`python3 -B -m unittest deployment.test_transport_syntax deployment.test_firecrawl_approval deployment.test_firecrawl_candidate deployment.test_firecrawl_patch deployment.test_research_update deployment.test_fork_sync deployment.test_release_selection`
and check shell syntax with `bash -n deployment/firecrawl_update.sh`. From the
repository root, with the API's TypeScript dependency on Node's module path, run
`NODE_PATH="$PWD/apps/api/node_modules" node --test deployment/self-hosted-monitor/judge.test.cjs`
for local-socket and cloud-boundary judge regressions. After installation on
GA401, `firecrawl-upgrade --check-only` exercises the installed guard plus real
search and scrape without initiating an update.
