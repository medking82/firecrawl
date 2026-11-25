# Search sidecar (GA401)

The Firecrawl core no longer loads search code. It uses only its built-in
SearXNG provider, with `SEARXNG_ENDPOINT=http://search-worker:8081`. The worker
serves a SearXNG-compatible `GET /search` and keeps the existing order: DDG
first, then the real SearXNG, both through `search-egress` on the internal
search network. Core upgrades therefore replace only the API image; the worker
keeps its own qualified image.

Public entry points do not change: `127.0.0.1:3003` (search continuity proxy,
Exa fallback) in front of the core on `127.0.0.1:3002`.

## Files

- `runtime/`: the exact files mounted from `/opt/firecrawl-search/`. Only
  `search-worker.cjs` differs from the previous preload release; it adds
  `GET /search` and still serves `POST /v2/search` for the preload profile.
- `docker-compose.override.sidecar.yaml`: GA401 override for this profile.
  The API loses `NODE_OPTIONS`, `SEARCH_WORKER_URL`, `SEARCH_TRACE` and both JS
  mounts. `html.duckduckgo.com` resolves to `127.0.0.1` inside the API, so the
  core's built-in DDG fallback fails at once instead of bypassing egress.
- `docker-compose.override.preload.yaml`: the previous override, kept as the
  exact rollback target.
- `deploy_search_sidecar.py`: `apply`, `rollback`, `status`. Run as root on
  GA401 from a staged copy of this directory.

The updater admits both profiles through
`../firecrawl-updater/deployment/search_runtime_contract.py`. In the sidecar
profile it keeps the live `search-worker` image as a protected rollback image
instead of replacing it with the candidate core image.

## Known differences from the preload profile

- The core sends SearXNG no result count; it assumes 20 rows per page and
  trims to `limit`. The worker therefore returns up to 20 rows per page. Page p
  uses SearXNG pages 3p-2..3p (about 7 rows each) and stops at 20 rows; page 1
  starts with one DDG page (10 rows). A short page means the engines had no
  more rows.
- The core's SearXNG request carries no `tbs` or `country`. While this
  profile is active, the continuity proxy appends them to the query as
  `fcctl:tbs=...` and `fcctl:country=...` (it detects the profile from the
  live Compose override); the worker removes the tokens before searching.
  `tbs` (`qdr:h/d/w/m/y`, or `cdr:1,cd_min:M/D/YYYY,cd_max:M/D/YYYY`) becomes
  DDG's `df` (an hour widens to a day; custom ranges are exact) and SearXNG's
  `time_range` (custom ranges use the smallest range reaching `cd_min`).
  Yahoo, the working SearXNG engine, has no year filter. `country` sets DDG's
  region only: a SearXNG region moves Yahoo to a regional host that
  search-egress does not allow. Exa fallback still skips `tbs`/`country`
  requests.
- A client disconnect no longer cancels the worker; the worker still stops at
  its 12 s deadline.

## Deploy and roll back

Install the updater that knows both profiles first; an older updater rejects
the sidecar profile.

```sh
sudo python3 -I -B deploy_search_sidecar.py status
sudo python3 -I -B deploy_search_sidecar.py apply      # backup under ~marck/.local/state/firecrawl-updater/search-sidecar/
sudo python3 -I -B deploy_search_sidecar.py rollback   # newest backup, or --backup DIR
```

`apply` from an earlier sidecar worker release (listed in
`PREVIOUS_SIDECAR_WORKERS`) replaces only the worker file and recreates only
`search-worker`; `rollback` restores it the same way. The updater contract
admits one sidecar worker hash, so deploy the worker first and then install the
matching updater; after a rollback, reinstall the previous updater from the
installer's preimage backup.

Both actions hold the updater `run.lock` and `update.lock`, recreate only
`api` and `search-worker` with the live Compose files and exact image IDs, and
verify the live API environment, mounts and host entries. A failed `apply`
restores the backup automatically.
