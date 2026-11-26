# GA401 Self-hosted Browser And Interact

An isolated Firecrawl Browser API supplies browser lifecycle, authenticated
application state and scrape replay. Its release selects the Browser API,
PostgreSQL, Redis, RabbitMQ and Playwright / Chromium images. Startup uses
locally loaded images and does not use Firecrawl Cloud keys or billing.

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

For the single-URL Firecrawl MCP, the search-continuity proxy behind `13002`
(GA401 `3003`) sends `/v2/browser*`, `/v2/interact*` and
`/v2/scrape/:scrapeId/interact` here with this tenant's key. The core API keeps
no scrape context, so the proxy remembers each core scrape ID for 24 hours (in
memory, at most 2048) and the first interact loads the same URL once on this
service with `formats: ["markdown"]`, `blockAds: false`, `maxAge: 0` and only
`waitFor`, `actions`, `headers`, `timeout` and `skipTlsVerification` from the
original. Later interact and stop calls with the core ID reuse that scrape. IDs
the proxy never saw pass through unchanged; after a proxy restart, scrape again.
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

For v2.11.626, the browser overlay forwards upstream's zero-data-retention
flag to Hangar. The local manager cannot supply ZDR-only proxy exits, so local
ZDR session creation fails closed. This compatibility change has not itself
established acceptance against a v2.11.626 runtime image.

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

For a manager-only patch, package its reviewed files and `manager-manifest.json`
under a new `/home/marck/services/firecrawl-browser/manager-releases/<id>/` root.
Run `activate-manager.py --expected-manager-sha256 <reviewed-current-hash>` from
that release only after CI passes. It defers if sessions are active, creates an
exclusive admission marker, then freezes the old manager before the final idle
check. The freeze also protects the first upgrade from legacy code which lacks
the marker guard, including existing keep-alive connections. If work raced in,
the old process resumes and activation defers. Otherwise the frozen idle process
is terminated without resuming queued admissions, and the new manager starts
with admission still blocked until readiness. The marker is then removed.
It changes only the manager unit's executable path while idle.
The API, CLI bridge, databases, images and existing core/monitor services stay in
place. `deployment.json` records separate `manager_release` and API `release` IDs.

For a new installation, run `python3 -B deployment/self-hosted-browser/deploy.py
--release-inputs` to list the required files and image keys. Package those files
from the reviewed release, including its matching queue schema at the package
root as `nuq.sql`. Load the reviewed Browser API, session,
PostgreSQL, Redis and RabbitMQ images into Docker. Prepare `release-images.json`
with `service` set to `browser`, the full extension API `source_commit`, `files` mapping
package-relative paths to SHA-256 hashes, and `images` mapping the listed keys
to full local `sha256:` image IDs. Keep the reviewed manifest with the release.

From the packaged release, run `python3 -B deployment/self-hosted-browser/deploy.py
--release-images release-images.json --check-images`. This verifies the package
files and cached image IDs without starting services. Remove `--check-images`
to install, or rerun with the same manifest to resume a partial installation.
The installer uses the reviewed images without a core checkout, core image,
registry pull or local build. It applies SQL to the isolated DB and compares the
production services it found before the install. It rejects unowned projects
and image changes to existing installations. Use the existing upgrade owner
for a cutover. Existing installations with a saved `BASE_API` can still use
their archived source overlay build procedure.

`smoke.py` exercises real API sessions, public navigation, persistent DOM, failures,
timeouts, auth/tenant ownership, network and credential isolation, unsupported
options, idle/hard TTL, aliases, scrape replay, real Codex-generated browser action,
post-action verification and idempotent cleanup. It cleans only its returned IDs
and temporary tenant. Receipts are private under `shared/`.

CDP TCP errors now clean up only that connection's Docker exec transport. They do
not stop its browser container or restart the manager. `cdp-transport.test.cjs`
uses actual TCP RST against the production upgrade handler in an isolated process
and verifies the same peer transport remains alive and the reset transport exits.
`cdp-rst-smoke.cjs` repeats the scenario with two actual authenticated browser
sessions, a persistent peer CDP connection and a retained DOM proof marker; use
`--remote` on GA401, or its Windows default through the two SSH tunnels.

Focused contracts:

```text
node --test deployment/self-hosted-browser/policy.test.cjs
node --test deployment/self-hosted-browser/cdp-transport.test.cjs
python3 -B -m unittest discover -s deployment/self-hosted-browser -p test_codex_browser.py
python3 -B -m unittest discover -s deployment/self-hosted-monitor -p test_codex_judge.py
```

The optional DB contract requires `BROWSER_JOB_TEST_DATABASE_URL` naming a fresh
`firecrawl_browser_test*` database and refuses an existing job-store table. CI
creates this disposable DB. Mock/syntax checks alone do not establish runtime
success; use the native smoke receipt and the Windows API/CDP transport check.
