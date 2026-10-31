# Isolated Self-Hosted Monitor

This overlay runs Firecrawl **v2.11.621 / 33f295dc** native monitor controllers,
scheduler, RabbitMQ consumers, scrape workers, diff calculation and lifecycle API.
It does not switch the existing Firecrawl deployment to DB authentication.

## Deployment Boundary

- Separate Compose project `firecrawl-monitor`; separate persistent PostgreSQL,
  Redis, RabbitMQ and artifact volumes. The application database is `monitor`;
  NuQ's pinned upstream schema runs in this project's `postgres` database.
- API binds GA401 loopback **3006**. Windows uses a separate hidden SSH tunnel
  **13006 → ga401:3006**. Existing **13002 → 3002** stays unchanged.
  The search-continuity proxy behind 13002 (GA401 `3003`) also sends
  `/v2/monitor*` here with this tenant's key, so the single-URL Firecrawl MCP
  reaches monitors; the 13002 core API itself is unchanged.
- Real UUID API keys authenticate against local `teams` / `api_keys` and auth
  SQL functions. `teams.org_id` permits NULL locally, which keeps the native
  runner outside external credit locks. `bill_team_7` records local usage;
  its original signature has no idempotency key or prepaid quota enforcement.
- Scheduler claims use row locks, `SKIP LOCKED` and expiring leases. Expired
  work may be redelivered; the native check state machine handles recovery.
  This is not an exactly-once delivery guarantee.
- Local artifacts are private, atomically written JSON files in a Docker
  volume. This opt-in backend also suppresses the separate Cloud Bigtable
  change-tracking index write. The native monitor's own baseline stays intact.
- Goal judging uses a private Unix socket to GA401's actual native **Codex CLI**.
  It inherits the selected native model, reasoning effort and Fast/service tier
  (verified `gpt-6.1-sol` / `max`), with existing ChatGPT subscription auth.
  The per-call text-only model catalog disables shell, apply-patch and other
  tools; hooks, plugins, MCP, skills and search are disabled. User configuration
  and the cached model catalog are not modified. No paid API key or local GPU
  model is added. There is no automatic Claude/provider fallback. One judge runs
  at a time; failures keep the native low-confidence fallback visible.
- No Cloud Firecrawl fallback, paid API provider or GPU model is configured.

## Install And Validate

Package this directory, `apps/nuq-postgres/nuq.sql`, and the six source files
listed by `compile.cjs`, preserving their repository-relative paths in a new
immutable `/home/marck/services/firecrawl-monitor/releases/<content-id>` directory.
On GA401 run `python3 -B deployment/self-hosted-monitor/deploy.py` from that
release. It pins existing cached image IDs, builds an offline overlay, applies
the queue/application schemas, runs DB contracts on first install, seeds a
private local credential, and starts the independent stack. It checks production
configuration hashes and container start timestamps. A failed monitor readiness
check stops only the isolated API.

Private state is in `/home/marck/services/firecrawl-monitor/shared/`; never
commit its `.env`, credentials, databases, page content or logs. Windows' client
credential is copied to `%USERPROFILE%\.local\state\firecrawl-monitor\client.json`
with a user-only ACL. Its API URL must equal `http://127.0.0.1:13006`.

The separate Windows tunnel uses the existing `agent-sop-kit` owner:

```powershell
pwsh -File scripts/install-firecrawl-tunnel.ps1 -Execute -LocalPort 13006 -RemotePort 3006 `
  -TaskName 'Firecrawl Monitor SSH Tunnel to ga401' `
  -WrapperRoot "$env:USERPROFILE\.local\bin\firecrawl-monitor"
```

Run `python3 -B deployment/self-hosted-monitor/smoke.py` on GA401. It tests real
HTTP authentication, schema rejection, tenant isolation, create/run/results,
new/same/changed detection, persisted diffs, Codex CLI judgment, pause/resume,
scheduled execution, delete and 404. Its public fixture uses a stable HTTP Range
endpoint with a controlled Range header change; it does not edit an external
website. It also ages its own real check history, runs the retention service,
verifies the current snapshot hash is unchanged, then proves the next real
check is `same`. It deletes its own monitor and temporary tenant in `finally`.

`test-db.sql` additionally tests two concurrent PostgreSQL sessions; its header
documents the phases. Run it only on a disposable DB with no monitor rows.
`storage.test.cjs` runs during the offline overlay build. Syntax transpilation
is not a full upstream TypeScript/knip/Vitest run; the focused deployed-runtime
tests are the acceptance evidence for this overlay.

## Operating Limits And Rollback

The validated initial profile is bounded Markdown page monitoring and optional
CLI goal judging. Search/crawl monitors, JSON extraction, email, Slack and
external webhook delivery require their own dependencies and acceptance tests.
They are not enabled by a passing page-monitor test. No user notification
recipient is configured by deployment or smoke tests.

`firecrawl-monitor-prune.timer` runs hourly (up to two minutes randomized delay).
It expires terminal check history according to each monitor's `retentionDays`,
retains the latest check and all current page baselines regardless of age, and
skips any monitor with a queued/running check. Monitor row locks also block
concurrent FK check inserts until a prune transaction commits. Files are deleted
only through a durable GC outbox after global baseline/history reference checks;
protected entries are re-evaluated after an hour and failed unlinks stay queued.
Deleted monitors release their baselines after their own retention grace.
Unknown/untracked files are not swept. This is retention of monitor-owned
artifacts, not a general filesystem cleaner or an exact-deadline erasure promise.

`retention.test.cjs` runs against a temporary real PostgreSQL DB and temporary
filesystem. It covers old baseline protection, recent history, busy checks,
the FK insertion race, failed unlink recovery, symlink rejection and deleted
monitor grace. The focused test and Codex adapter contracts also run in PR CI.
For an operator preview, run `docker exec firecrawl-monitor-api-1 node
/app/monitor-prune.cjs --dry-run`; it changes neither metadata nor files.
Upstream Firecrawl updates do not automatically rebuild this pinned overlay.

To stop the feature, stop the `firecrawl-monitor` Compose project and
`firecrawl-monitor-judge.service` and `firecrawl-monitor-prune.timer`; disable
its separate Windows tunnel task.
Do not remove volumes or shared credentials during rollback. Production
Firecrawl, its updater and its original tunnel are independent of these actions.
