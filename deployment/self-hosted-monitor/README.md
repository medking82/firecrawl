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
- Goal judging uses a private Unix socket to GA401's existing OpenCodex
  `claude-cli/claude-sonnet-5-5` route. That adapter disables CLI tools and MCP;
  page text cannot execute CLI tools. No API key, provider account or model
  workload is added. Existing subscription limits still apply. One judge runs
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
new/same/changed detection, persisted diffs, CLI judgment, pause/resume,
scheduled execution, delete and 404. Its public fixture uses a stable HTTP Range
endpoint with a controlled Range header change; it does not edit an external
website. It deletes its own monitor and temporary tenant in `finally`.

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

The API accepts upstream `retentionDays`, but this initial filesystem adapter
does **not** implement timed artifact pruning. Disk usage and private backups
need operator management; do not promise automatic deletion at that deadline.
Upstream Firecrawl updates do not automatically rebuild this pinned overlay.

To stop the feature, stop the `firecrawl-monitor` Compose project and
`firecrawl-monitor-judge.service`; disable its separate Windows tunnel task.
Do not remove volumes or shared credentials during rollback. Production
Firecrawl, its updater and its original tunnel are independent of these actions.
