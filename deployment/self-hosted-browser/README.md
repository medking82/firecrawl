# GA401 Self-hosted Browser And Interact

An isolated native Firecrawl API overlay supplies browser lifecycle, authenticated
application state and scrape replay without modifying the existing core or monitor
projects. It reuses GA401's cached Firecrawl v2.11.621, PostgreSQL, Redis, RabbitMQ
and Playwright 1.64.1 / Chromium images. Build and startup never pull images or use
Firecrawl Cloud keys, credits, billing providers or a Cloud fallback.

## Endpoints And State

| Purpose | GA401 Loopback | Windows Tunnel |
| --- | --- | --- |
| Native API | `3007` | `13007` |
| Token-protected CDP WebSocket | `3008` | `13008` |

The primary `13002` endpoint and monitor `13006` endpoint keep their existing
configuration. Windows `FIRECRAWL_API_URL` remains `http://127.0.0.1:13002`.
Browser calls explicitly load the private
`%USERPROFILE%\.local\state\firecrawl-browser\client.json`, verify its `api_url`
equals `http://127.0.0.1:13007`, and send its API key only as a bearer header.
Do not print credentials or CDP capability URLs, or put credentials in argv.

Host releases live below `/home/marck/services/firecrawl-browser/releases/`;
private state is in the sibling `shared/` directory. Compose project
`firecrawl-browser` owns its own application/queue databases, Redis, RabbitMQ
and artifact volume. `001-monitor.sql` supplies the existing reviewed local auth
and usage-ledger RPCs; `001-browser.sql` supplies native session/activity/profile
tables; `002-job-store.sql` replaces the Bigtable access and scrape-state stores
only when `SELF_HOSTED_BROWSER_SOCKET` is set. Native tenant ownership, retention
validation, replay and billing settlement remain in the API controllers.
Access/replay state expires logically after 24 hours; this deployment does not
promise physical erasure of expired rows or snapshots at that deadline.

## Supported Contract

- Native `/v2/browser` create/list/status/execute/delete, including `/v2/interact`
  aliases; native `/v2/scrape/:scrapeId/interact` replay, execute and delete.
- Node async Playwright code with persistent `page`, `context`, `browser` and
  `require`; local cookies and page state last for the session lifetime. Node
  code runs in a disposable container, never in the host supervisor.
- Natural-language scrape-linked interaction defaults to native Codex CLI and
  existing ChatGPT subscription auth, inheriting native model/effort/service tier.
  The text-only CLI adapter has no tools, hooks, plugins, MCP, browser, skills or
  search. It proposes Playwright code; the API executes that code in the session,
  observes the result and asks for the next decision. At most five decisions and
  four actions share an overall `min(request timeout, 120s)` deadline. Failures
  remain visible; no Claude/Antigravity/API-key/provider fallback occurs.
- Hard TTL (30–3600 seconds), idle TTL (10–3600 seconds), execution timeout,
  bounded output, stop, restart recovery and native settlement. At most two
  active/starting containers; each has 1 CPU, 1 GiB memory and a PID limit.

Python, Bash/agent-browser CLI, live view, recordings, persistent saved profiles,
extensions/ad blocking, country routing and stealth/mobile proxies are **not
implemented**. Requests for these options fail closed. Local browser defaults
are `streamWebView: false` and `blockAds: false`; explicitly set `blockAds: false`
on the original scrape when planning to interact with it. Use the native HTTP
API if an installed CLI assumes hosted-only defaults.

## Isolation And Cleanup

`firecrawl-browser-manager.service` is the trusted lifecycle supervisor. Its
private Unix socket is mounted read-only in the authenticated API. The execution
containers have no host credentials, Docker socket, host network or service
volumes. They use the cached Playwright image, non-root UID 1000, read-only root,
tmpfs, dropped capabilities, default Docker seccomp and `no-new-privileges`.

Containers use `network=none`. Chromium reaches only a Unix-socket forward proxy
through a loopback relay. The proxy resolves and pins public IPv4 addresses and
permits HTTP 80 / HTTPS CONNECT 443; private, loopback, metadata, special-use and
IPv6 destinations are rejected. Arbitrary Node networking cannot bypass the
container network namespace. CDP is piped through a separate `docker exec`
transport, rather than exposing a container port or a writable host socket.

Stop and expiration remove the exact session container and tmpfs, invalidate its
CDP token and preserve a small stopped receipt for DB settlement. A supervisor
restart ends its in-flight sessions and removes only containers carrying its
dedicated session label. It never resurrects stale capability URLs. The manager
retains stopped metadata receipts; there is no age-only sweep of service volumes.

## Deployment And Validation

Package the reviewed source into a new immutable release. Run `python3 -B
deployment/self-hosted-browser/deploy.py` on GA401 from that release. It snapshots
all existing core/monitor image IDs and start times and production config hashes,
pins cached images, builds offline, applies SQL only to its isolated DB, installs
the two user services, starts the separate API and compares the original services.
The deployment refuses an unowned existing Compose project. Treat the lifecycle
manager's Docker access as privileged; do not expose its private HTTP socket.

`smoke.py` exercises real API sessions, public navigation, persistent DOM, failures,
timeouts, auth/tenant ownership, network and credential isolation, unsupported
options, idle/hard TTL, aliases, scrape replay, real Codex-generated browser action,
post-action verification and idempotent cleanup. It cleans only its returned IDs
and temporary tenant. Receipts are private under `shared/`.

Focused contracts:

```text
node --test deployment/self-hosted-browser/policy.test.cjs
python3 -B -m unittest discover -s deployment/self-hosted-browser -p test_codex_browser.py
python3 -B -m unittest discover -s deployment/self-hosted-monitor -p test_codex_judge.py
```

The optional DB contract requires `BROWSER_JOB_TEST_DATABASE_URL` naming a fresh
`firecrawl_browser_test*` database and refuses an existing job-store table. CI
creates this disposable DB. Mock/syntax checks alone do not establish runtime
success; use the native smoke receipt and the Windows API/CDP transport check.
