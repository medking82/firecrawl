#!/usr/bin/env bash
# Adopted from the installed GA401 updater, SHA256
# b0eaaa79ee3c85766057ef0cc46807bb4e657711df78376ee5561f3feba34ab0.
# Keep upstream patch tracking; Firecrawl owns its transport admission.
set -Eeuo pipefail
umask 077

repo=${FIRECRAWL_REPO:-/home/marck/github/firecrawl}
series=${FIRECRAWL_UPDATE_SERIES:-v2.11}
canonical_state_dir=/home/marck/.local/state/firecrawl-updater
state_dir=${FIRECRAWL_UPDATE_STATE_DIR:-$canonical_state_dir}
guard_sha256=07f73956c9531c483ccd9816af9b3aa06de87424dc0ab33758e9daa2c5e16c3d
guard_file="$canonical_state_dir/guards/$guard_sha256.py"
worktree_root=${FIRECRAWL_WORKTREE_ROOT:-/home/marck/github/worktrees/firecrawl}
security_refresh=${FIRECRAWL_SECURITY_REFRESH:-/home/marck/.local/lib/firecrawl-security-refresh/deployment/self-hosted-auto/security_refresh.py}
security_journals=${FIRECRAWL_SECURITY_STATE:-/home/marck/.local/state/firecrawl-security-refresh}
notice_repo=${FIRECRAWL_NOTICE_REPO:-medking82/pc-maintenance}
notice_owner=medking82
previous_commit=""
previous_tag=""
rollback_active=0
deployment_started=0
preimage=""
candidate_dir=""
candidate_evidence=""
candidate_base_image=""
candidate_checks_sha256=""
phase=""
retired_agents=0

log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"; }

# Embedded in the updater by build-updater.py. Caller holds updater run.lock.
sync_fork_main() {
  local sync_dir work_dir origin_ref upstream_ref origin_oid upstream_oid merged_oid remote_origin remote_upstream prior
  for prior in "$state_dir"/fork-sync.*; do
    [[ -d "$prior/worktree" ]] || continue
    if [[ ! -f "$prior/snapshots" ]] || ! grep -qx 'status=complete' "$prior/snapshots"; then
      log "prior fork sync requires inspection; preserved $prior"
      return 1
    fi
  done
  sync_dir=$(mktemp -d "$state_dir/fork-sync.XXXXXXXX") || return 1
  work_dir="$sync_dir/worktree"
  origin_ref="refs/firecrawl-updater/$(basename "$sync_dir")/origin"
  upstream_ref="refs/firecrawl-updater/$(basename "$sync_dir")/upstream"
  # Never mutate a branch or the running checkout; preserve exact fetched tips.
  git fetch --no-tags origin "refs/heads/main:$origin_ref" || return 1
  git fetch --no-tags upstream "refs/heads/main:$upstream_ref" || return 1
  origin_oid=$(git rev-parse --verify "$origin_ref^{commit}") || return 1
  upstream_oid=$(git rev-parse --verify "$upstream_ref^{commit}") || return 1
  printf 'origin=%s\nupstream=%s\n' "$origin_oid" "$upstream_oid" >"$sync_dir/snapshots"
  git worktree add --detach "$work_dir" "$origin_oid" || return 1
  if git -C "$work_dir" merge-base --is-ancestor "$upstream_oid" "$origin_oid"; then
    merged_oid=$origin_oid
  elif git -C "$work_dir" merge-base --is-ancestor "$origin_oid" "$upstream_oid"; then
    git -C "$work_dir" -c core.hooksPath=/dev/null merge --ff-only "$upstream_oid" || return 1
    merged_oid=$(git -C "$work_dir" rev-parse HEAD) || return 1
  else
    if ! git -C "$work_dir" -c core.hooksPath=/dev/null -c user.name='Firecrawl Updater' -c user.email='firecrawl-updater@localhost' \
      merge --no-edit --no-ff "$upstream_oid"; then
      log "fork sync conflict or merge failure; preserved $sync_dir"
      return 1
    fi
    merged_oid=$(git -C "$work_dir" rev-parse HEAD) || return 1
    [[ $(git -C "$work_dir" rev-parse 'HEAD^1') == "$origin_oid" && $(git -C "$work_dir" rev-parse 'HEAD^2') == "$upstream_oid" ]] || return 1
  fi
  [[ -z $(git -C "$work_dir" status --porcelain --untracked-files=all) ]] || return 1
  git merge-base --is-ancestor "$origin_oid" "$merged_oid" || return 1
  git merge-base --is-ancestor "$upstream_oid" "$merged_oid" || return 1
  printf 'merged=%s\n' "$merged_oid" >>"$sync_dir/snapshots"
  # Re-read both remote tips: a changed input requires a fresh run, never a force push.
  remote_origin=$(git ls-remote --exit-code origin refs/heads/main) || return 1
  remote_upstream=$(git ls-remote --exit-code upstream refs/heads/main) || return 1
  [[ "$remote_origin" == "$origin_oid"$'\t'refs/heads/main && "$remote_upstream" == "$upstream_oid"$'\t'refs/heads/main ]] || {
    log "fork sync remote moved; preserved $sync_dir"; return 1;
  }
  if [[ "$merged_oid" != "$origin_oid" ]]; then
    git push origin "$merged_oid:refs/heads/main" || { log "fork sync push rejected; preserved $sync_dir"; return 1; }
  fi
  git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main || return 1
  [[ $(git rev-parse refs/remotes/origin/main) == "$merged_oid" ]] || { log "fork changed after push; preserved $sync_dir"; return 1; }
  printf 'status=complete\n' >>"$sync_dir/snapshots"
  git worktree remove "$work_dir" || return 1
  git update-ref -d "$origin_ref" "$origin_oid" || return 1
  git update-ref -d "$upstream_ref" "$upstream_oid" || return 1
  log "fork sync complete; evidence $sync_dir/snapshots"
}

validate_worktree() {
  local changes
  changes=$(git status --porcelain --untracked-files=no) || return 1
  [[ -z "$changes" ]] && return 0
  # The user retired this root agent notice; no runtime edit is exempt.
  if [[ "$changes" == ' D AGENTS.md' && ! -e AGENTS.md && ! -L AGENTS.md ]]; then
    retired_agents=1
    return 0
  fi
  return 1
}

preserve_retired_notice() {
  [[ "$retired_agents" -eq 1 ]] || return 0
  [[ ! -L AGENTS.md ]] || return 1
  [[ ! -e AGENTS.md || -f AGENTS.md ]] || return 1
  rm -f -- AGENTS.md
}

validate_config() {
  local rendered
  rendered=$(mktemp)
  if ! docker compose config --format json >"$rendered"; then
    rm -f "$rendered"
    return 1
  fi
  if ! python3 -B - "$rendered" <<'PY'
import json
import sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
ports = config["services"]["api"].get("ports", [])
api_ports = [port for port in ports if str(port.get("published")) == "3002"]
valid = bool(api_ports) and all(str(port.get("target")) == "3002"
                              and port.get("host_ip") == "127.0.0.1" for port in api_ports)
valid = valid and all(port.get("host_ip") == "127.0.0.1" for port in ports if port.get("published") is not None)
if not valid:
    raise SystemExit("API port 3002 is not bound exclusively to 127.0.0.1")
PY
  then
    rm -f "$rendered"
    return 1
  fi
  rm -f "$rendered"
}

smoke_test() {
  local ready=0
  for _ in $(seq 1 30); do
    if curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:3002/v0/health/readiness >/dev/null; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" -ne 1 ]]; then
    log "readiness failed"
    return 1
  fi

  # Run only Firecrawl-owned checks against the self-hosted loopback endpoint.
  python3 -B - "$guard_file" "$guard_sha256" 'http://127.0.0.1:3002' <<'PYSMOKE'
import hashlib, json, os, stat, sys, types, urllib.request
from pathlib import Path

path, expected, endpoint = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
failure = 'guard_unavailable'
try:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError()
        source = stream.read(65537)
    if len(source) > 65536 or hashlib.sha256(source).hexdigest() != expected:
        raise ValueError()
    guard = types.ModuleType('firecrawl_updater_guard')
    exec(compile(source, str(path), 'exec'), guard.__dict__)
    failure = 'guard_rejected'
    verified = guard.verify()
    if not isinstance(verified, dict) or verified.get('verified') is not True:
        raise ValueError()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def post(route, body):
        request = urllib.request.Request(endpoint + route, data=json.dumps(body).encode(),
                                         headers={'Content-Type':'application/json'}, method='POST')
        with opener.open(request, timeout=75) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('success') is not True:
            raise ValueError()
        return value.get('data')
    failure = 'search_failed'
    result = post('/v2/search', {'query':'Firecrawl documentation','limit':1})
    if not isinstance(result, dict) or not isinstance(result.get('web'), list) or not result['web']:
        raise ValueError()
    failure = 'scrape_failed'
    result = post('/v2/scrape', {'url':'https://example.com','formats':['markdown'],
                  'fastMode':True,'timeout':60000,'maxAge':0,'storeInCache':False})
    if (not isinstance(result, dict) or not isinstance(result.get('markdown'), str)
            or not result['markdown'].strip() or (result.get('metadata') or {}).get('statusCode') != 200):
        raise ValueError()
except Exception:
    raise SystemExit('Firecrawl smoke failed: ' + failure) from None
print('Firecrawl transport, search and scrape smoke passed')
PYSMOKE
}

set_restart_policy() {
  # This check precedes release admission: an unknown future tag must never
  # prevent boot recovery for the currently deployed services.
  python3 -B - <<'PYRESTART'
import json, subprocess
required = ('api', 'nuq-postgres', 'playwright-service', 'redis', 'rabbitmq', 'foundationdb')
config = json.loads(subprocess.check_output(['docker', 'compose', 'config', '--format', 'json'], timeout=30))
for name in required:
    if config['services'][name].get('restart') != 'unless-stopped':
        raise SystemExit('Persistent Firecrawl restart override is missing')
    ids = subprocess.check_output(['docker','compose','ps','--all','-q',name], text=True, timeout=10).split()
    if len(ids) != 1:
        raise SystemExit('Expected exactly one existing Firecrawl service container')
    subprocess.run(['docker','update','--restart','unless-stopped',ids[0]], check=True, stdout=subprocess.DEVNULL, timeout=15)
    actual = subprocess.check_output(['docker','inspect','--format','{{.HostConfig.RestartPolicy.Name}}',ids[0]], text=True, timeout=10).strip()
    if actual != 'unless-stopped': raise SystemExit('Restart policy verification failed')
PYRESTART
}

prepare_candidate() {
  # Git-owned candidate checkout; never build in or switch the live checkout.
  candidate_dir="$worktree_root/update-$latest_commit"
  candidate_evidence=$(mktemp -d "$state_dir/candidate.XXXXXXXX")
  if [[ -e "$candidate_dir" ]]; then
    [[ ! -L "$candidate_dir" && "$(git -C "$candidate_dir" rev-parse HEAD)" == "$latest_commit" && -z "$(git -C "$candidate_dir" status --porcelain)" ]] || return 1
  else
    mkdir -p "$(dirname "$candidate_dir")"
    git worktree add --detach "$candidate_dir" "$latest_commit"
  fi
  # Fixed project identity keeps the rendered non-build configuration comparable.
  # Build changes only local image tags; the running containers keep exact IDs.
  docker compose --project-name firecrawl --env-file "$repo/.env" \
    -f "$candidate_dir/docker-compose.yaml" -f "$repo/docker-compose.override.yaml" build --build-arg "GIT_SHA=$latest_commit"
  [[ "$(git -C "$candidate_dir" rev-parse HEAD)" == "$latest_commit" && -z "$(git -C "$candidate_dir" status --porcelain)" ]] || return 1
  # Private build context contains only reviewed, embedded patch inputs. The base
  # is the immutable ID of the just-built candidate, never a mutable tag.
  local patch_context="$candidate_evidence/patch-context" base_image patched_image base_tag
  mkdir -m 700 "$patch_context"
  cat >"$patch_context/Dockerfile" <<'PATCH_DOCKERFILE'
ARG BASE_API
FROM ${BASE_API}
ARG BASE_API
ARG BASE_IMAGE_ID
RUN --mount=type=bind,source=patch-lock.yaml,target=/tmp/patch-lock.yaml \
    --mount=type=bind,source=patch.cjs,target=/tmp/patch.cjs \
    --mount=type=bind,source=patches.json,target=/tmp/patches.json \
    node /tmp/patch.cjs
PATCH_DOCKERFILE
  cat >"$patch_context/patch.cjs" <<'PATCH_SCRIPT'
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const https = require('node:https');
const zlib = require('node:zlib');

function fail(message) { throw new Error(message); }
function version(s) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(s)) fail(`unsupported version: ${s}`);
  return s.split('.').map(Number);
}
function range(expression) {
  const parts = expression.split(' ');
  if (parts.length !== 2 || !parts[0].startsWith('>=') || !parts[1].startsWith('<')) fail('range must be >=X.Y.Z <X.Y.Z');
  const lo = version(parts[0].slice(2)), hi = version(parts[1].slice(1));
  const cmp = (a, b) => a[0]-b[0] || a[1]-b[1] || a[2]-b[2];
  if (cmp(lo, hi) >= 0) fail('empty affected range');
  return s => { const v = version(s); return cmp(v, lo) >= 0 && cmp(v, hi) < 0; };
}
function integrity(lock, name, target) {
  const escaped = `${name}@${target}`.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const packages = lock.split(/^snapshots:\s*$/m)[0];
  const matches = [...packages.matchAll(new RegExp(`^  ['\"]?${escaped}['\"]?:\\r?\\n    resolution: \\{integrity: (sha512-[A-Za-z0-9+/]+={0,2})\\}`, 'gm'))];
  if (matches.length !== 1) fail('expected one lockfile package integrity');
  return matches[0][1];
}
function archive(buffer) {
  const raw = zlib.gunzipSync(buffer, {maxOutputLength: 50 * 1024 * 1024});
  const files = new Map(); let pos = 0, ended = false;
  while (pos + 512 <= raw.length) {
    const h = raw.subarray(pos, pos + 512); pos += 512;
    if (h.every(x => x === 0)) { ended = true; break; }
    const str = (start, len) => h.subarray(start, start + len).toString('utf8').split('\0')[0];
    const oct = (start, len) => { const s = str(start, len).trim(); if (!/^[0-7]+$/.test(s)) fail('invalid tar number'); return parseInt(s, 8); };
    const sum = [...h].reduce((a, b, i) => a + (i >= 148 && i < 156 ? 32 : b), 0);
    if (sum !== oct(148, 8)) fail('invalid tar checksum');
    const prefix = str(345, 155), name = (prefix ? prefix + '/' : '') + str(0, 100);
    const size = oct(124, 12), type = String.fromCharCode(h[156] || 48), mode = oct(100, 8);
    if (pos + Math.ceil(size / 512) * 512 > raw.length) fail('truncated tar');
    if (!/^package\/(?:[^/]+\/?)*$/.test(name) || name.split('/').some(x => x === '.' || x === '..' || x.includes('\\'))) fail('unsafe tar path');
    const rel = name.slice(8).replace(/\/$/, '');
    if (!rel || (type !== '0' && type !== '5') || (type === '5' && size)) fail('unsupported tar entry');
    if (files.has(rel)) fail('duplicate tar entry');
    files.set(rel, {type, mode: mode & 0o777, data: raw.subarray(pos, pos + size)});
    pos += Math.ceil(size / 512) * 512;
  }
  if (!ended || !files.has('package.json') || files.get('package.json').type !== '0') fail('invalid tar archive');
  return files;
}
function scan(root, name, affected) {
  const targets = [], links = [], candidates = [], installations = new Map();
  const walk = dir => {
    for (const ent of fs.readdirSync(dir, {withFileTypes:true})) {
      const p = path.join(dir, ent.name);
      if (ent.isSymbolicLink()) { candidates.push(p); continue; }
      if (!ent.isDirectory()) continue;
      const manifest = path.join(p, 'package.json');
      if (fs.existsSync(manifest)) {
        let data; try { data = JSON.parse(fs.readFileSync(manifest, 'utf8')); } catch { fail(`invalid manifest: ${manifest}`); }
        if (data.name === name) {
          if (typeof data.version !== 'string') fail(`missing version: ${p}`);
          const isAffected = affected(data.version);
          const rel = path.relative(root, p).split(path.sep).join('/');
          const encoded = name.replace('/', '+');
          const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          const escapedEncoded = encoded.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          const locator = rel.match(new RegExp(`^node_modules/\\.pnpm/${escapedEncoded}@([0-9]+\\.[0-9]+\\.[0-9]+)(?:_[^/]+)?/node_modules/${escapedName}$`));
          if (!locator || (locator[1] !== data.version && !affected(locator[1]))) fail(`unknown package layout: ${p}`);
          installations.set(fs.realpathSync(p), data.version);
          if (isAffected) targets.push(p);
        }
      }
      walk(p);
    }
  };
  walk(root);
  for (const link of candidates) {
    let real; try { real = fs.realpathSync(link); } catch { if (path.basename(link) === name.split('/').at(-1)) fail(`broken package link: ${link}`); continue; }
    const installed = [...installations.keys()].find(p => real === p || real.startsWith(p + path.sep));
    if (installed) { links.push({link, target:real, version:installations.get(installed)}); continue; }
    const manifest = path.join(real, 'package.json');
    if (!fs.existsSync(manifest)) continue;
    let data; try { data = JSON.parse(fs.readFileSync(manifest, 'utf8')); } catch { fail(`invalid linked manifest: ${link}`); }
    if (data.name !== name) continue;
    fail(`linked package outside known pnpm layout: ${link}`);
  }
  return {targets, links, count: installations.size};
}
function extract(files, dest) {
  for (const [rel, entry] of files) {
    const out = path.join(dest, rel);
    if (entry.type === '5') fs.mkdirSync(out, {recursive:true});
    else { fs.mkdirSync(path.dirname(out), {recursive:true}); fs.writeFileSync(out, entry.data, {mode:entry.mode, flag:'wx'}); }
  }
}
async function download(url) {
  return new Promise((resolve, reject) => https.get(url, res => {
    if (res.statusCode !== 200) { res.resume(); reject(new Error(`registry HTTP ${res.statusCode}`)); return; }
    const chunks = []; let size = 0;
    res.on('data', chunk => { size += chunk.length; if (size > 20 * 1024 * 1024) res.destroy(new Error('archive too large')); else chunks.push(chunk); });
    res.on('end', () => resolve(Buffer.concat(chunks))); res.on('error', reject);
  }).on('error', reject));
}
async function main() {
  const {BASE_API:base, BASE_IMAGE_ID:baseId} = process.env;
  if (!/^firecrawl-updater-base:[a-zA-Z0-9_.-]+$/.test(base || '') || !/^sha256:[a-f0-9]{64}$/.test(baseId || '')) fail('verified local base tag and image ID required');
  const lock = fs.readFileSync('/tmp/patch-lock.yaml', 'utf8');
  const rawSpec = fs.readFileSync('/tmp/patches.json', 'utf8'), specs = JSON.parse(rawSpec);
  if (!Array.isArray(specs) || !specs.length) fail('patch list must be nonempty');
  const seen = new Set(), plans = [];
  for (const {name, affected:expr, target} of specs) {
    if (!/^(?:@[a-z0-9._-]+\/)?[a-z0-9._-]+$/.test(name || '') || seen.has(name)) fail('invalid or duplicate package name');
    seen.add(name);
    const affected = range(expr || ''); if (affected(target)) fail('target is affected');
    const expected = integrity(lock, name, target), before = scan('/app', name, affected);
    if (!before.count) fail(`package not found in supported layout: ${name}`);
    if (!before.targets.length) {
      console.log(JSON.stringify({base, baseId, name, status:'already-safe', installed:before.count}));
      continue;
    }
    const short = name.split('/').at(-1);
    const data = await download(`https://registry.npmjs.org/${name}/-/${short}-${target}.tgz`);
    if ('sha512-' + crypto.createHash('sha512').update(data).digest('base64') !== expected) fail('lockfile integrity mismatch');
    const files = archive(data), manifest = JSON.parse(files.get('package.json').data.toString());
    if (manifest.name !== name || manifest.version !== target) fail('archive identity mismatch');
    for (const p of before.targets) {
      const old = JSON.parse(fs.readFileSync(path.join(p, 'package.json'), 'utf8'));
      for (const key of ['dependencies', 'optionalDependencies', 'peerDependencies']) {
        const normalized = value => JSON.stringify(Object.entries(value || {}).sort(([a], [b]) => a.localeCompare(b)));
        if (normalized(old[key]) !== normalized(manifest[key])) fail(`dependency declaration changed: ${name} ${key}`);
      }
    }
    plans.push({name, expr, target, affected, expected, before, files});
  }
  for (const plan of plans) for (const p of plan.before.targets) { fs.rmSync(p, {recursive:true}); fs.mkdirSync(p); extract(plan.files, p); }
  for (const plan of plans) {
    const after = scan('/app', plan.name, plan.affected);
    if (after.targets.length || after.links.some(link => plan.affected(link.version))) fail(`affected packages remain: ${plan.name}`);
    if (JSON.stringify(plan.before.links.map(x => [x.link, x.target])) !== JSON.stringify(after.links.map(x => [x.link, x.target]))) fail(`package links changed: ${plan.name}`);
    console.log(JSON.stringify({base, baseId, name:plan.name, affected:plan.expr, target:plan.target, integrity:plan.expected, lockSha256:crypto.createHash('sha256').update(lock).digest('hex'), specSha256:crypto.createHash('sha256').update(rawSpec).digest('hex'), patched:plan.before.targets, links:after.links}));
  }
}
if (require.main === module) main().catch(e => { console.error(e.stack); process.exitCode = 1; });
module.exports = {range, integrity, archive, scan, extract};
PATCH_SCRIPT
  cat >"$patch_context/patches.json" <<'PATCH_SPEC'
[
  {"name": "decode-uri-component", "affected": ">=0.0.0 <0.5.1", "target": "0.5.1"},
  {"name": "systeminformation", "affected": ">=0.0.0 <5.33.16", "target": "5.33.18"}
]
PATCH_SPEC
  cat >"$patch_context/patch-lock.yaml" <<'PATCH_LOCK'
packages:
  decode-uri-component@0.5.1:
    resolution: {integrity: sha512-vWHQkXgg2cE3BB4rR0c+PYw9BCQzLoNIRMI7AunIkZst74tw/vpPp7VLoYtsR/cy1f55g3XDWUslfeCZjiZJyA==}
  systeminformation@5.33.18:
    resolution: {integrity: sha512-SF8DBQx3ypa8zDqZlezpHQ6M2g7TrqSEWpAtC4OsHCdLmi7J2WdxaSbA8/DJyEn2l/CHPrW0VFenoqoDccrw6g==}
snapshots:
PATCH_LOCK
  base_image=$(python3 -B - "$candidate_dir" "$repo" "$latest_commit" <<'PYBASE'
import json, re, subprocess, sys
source, repo, commit = sys.argv[1:]
compose = ['docker','compose','--project-name','firecrawl','--env-file',repo+'/.env','-f',source+'/docker-compose.yaml','-f',repo+'/docker-compose.override.yaml']
config = json.loads(subprocess.check_output(compose+['config','--format','json'], timeout=30))
api = config['services']['api']
if not api.get('build'): raise SystemExit('API must be built from the candidate')
image = api.get('image') or 'firecrawl-api'
info = json.loads(subprocess.check_output(['docker','image','inspect',image], timeout=30))[0]
image_id = info['Id']
if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id): raise SystemExit('Invalid candidate image ID')
env = dict(item.split('=',1) for item in info['Config'].get('Env',[]) if '=' in item)
if env.get('FIRECRAWL_BUILD_SHA') != commit: raise SystemExit('Candidate build SHA mismatch')
print(image_id)
PYBASE
  )
  base_tag="firecrawl-updater-base:$(basename "$candidate_evidence")"
  docker tag "$base_image" "$base_tag"
  if [[ "$(docker image inspect --format '{{.Id}}' "$base_tag")" != "$base_image" ]] ||
     ! DOCKER_BUILDKIT=1 docker build --pull=false --iidfile "$candidate_evidence/patched-iid" \
       --build-arg "BASE_API=$base_tag" --build-arg "BASE_IMAGE_ID=$base_image" "$patch_context"; then
    docker image rm "$base_tag" >/dev/null 2>&1 || true
    return 1
  fi
  docker image rm "$base_tag" >/dev/null
  candidate_base_image=$base_image
  patched_image=$(cat "$candidate_evidence/patched-iid")
  [[ "$patched_image" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  # Verify the resulting filesystem ancestry and inherited runtime metadata;
  # a tag race or a different resolved base must never reach admission.
  python3 -B - "$base_image" "$patched_image" <<'PYPATCHBASE'
import json, subprocess, sys
base, patched = (json.loads(subprocess.check_output(['docker','image','inspect',image], timeout=30))[0] for image in sys.argv[1:])
layers = base['RootFS']['Layers']
if not layers or patched['RootFS']['Layers'][:len(layers)] != layers:
    raise SystemExit('Patch base layers mismatch')
for key in ('Env','Cmd','Entrypoint','WorkingDir','User','Volumes','ExposedPorts','Labels','Healthcheck','StopSignal'):
    if base['Config'].get(key) != patched['Config'].get(key):
        raise SystemExit('Patch base configuration mismatch')
PYPATCHBASE
  python3 -B - "$guard_file" "$guard_sha256" "$candidate_dir" "$repo" "$latest_tag" "$latest_commit" "$candidate_evidence" "$patched_image" <<'PYCANDIDATE'
import hashlib, json, os, re, stat, subprocess, sys, types, uuid
from pathlib import Path
guard_path, guard_sha256, source, repo, tag, commit, evidence, patched_image = sys.argv[1:]
if not re.fullmatch(r'sha256:[0-9a-f]{64}', patched_image): raise SystemExit('Invalid patched image ID')
evidence = Path(evidence)
def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60, **kw).stdout
compose = ['docker','compose','--project-name','firecrawl','--env-file',repo+'/.env','-f',source+'/docker-compose.yaml','-f',repo+'/docker-compose.override.yaml']
def contract(config):
    # Build contexts differ between checkouts; all effective runtime fields must match.
    result = json.loads(json.dumps(config))
    for service in result['services'].values(): service.pop('build', None)
    return result
def digest(data): return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
report = {'schema':1,'status':'blocked','tag':tag,'source_commit':commit}
identity = None
try:
    config = json.loads(run(compose+['config','--format','json']))
    protected = {'nuq-postgres': 'firecrawl-nuq-postgres-1',
                 'playwright-service': 'firecrawl-playwright-service-1'}
    if not set(protected).issubset(config['services']): raise ValueError('protected_service_missing')
    images = {}
    for name, service in config['services'].items():
        if name in protected:
            fmt = '{"image":{{json .Image}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"project":{{json (index .Config.Labels "com.docker.compose.project")}}}'
            current = json.loads(run(['docker','inspect','--format',fmt,protected[name]]))
            if current.get('service') != name or current.get('project') != 'firecrawl': raise ValueError('protected_service_identity')
            image = current.get('image')
            if not isinstance(image,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image): raise ValueError('protected_image_identity')
        else:
            image = patched_image if name == 'api' else service.get('image') or 'firecrawl-'+name
        value = json.loads(run(['docker','image','inspect',image]))[0]
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',value['Id']): raise ValueError('image_identity')
        images[name] = {'image':value['Id'],'pull_policy':'never'}
    api = config['services']['api']
    # Admit only the observed, reviewed API start contract. The isolated probe
    # deliberately replaces the entrypoint, so it cannot verify these fields.
    forbidden = ('privileged','devices','cap_add','security_opt','pid','ipc',
                 'userns_mode','network_mode','user','tmpfs','cgroup_parent')
    fdb_mount = {'type':'volume','source':'fdb-cluster-file','target':'/var/fdb',
                 'read_only':True,'volume':{}}
    if (any(api.get(key) for key in forbidden)
            or api.get('working_dir','/app') != '/app'
            or api.get('volumes',[]) not in ([], [fdb_mount])):
        raise ValueError('unsupported_api_runtime')
    metadata = json.loads(run(['docker','image','inspect',images['api']['image']]))[0]['Config']
    command = api.get('command') if api.get('command') is not None else metadata.get('Cmd')
    entrypoint = api.get('entrypoint') if api.get('entrypoint') is not None else metadata.get('Entrypoint')
    if (command != ['node','dist/src/harness.js','--start-docker']
            or entrypoint != ['docker-entrypoint.sh']
            or metadata.get('WorkingDir') != '/app'
            or metadata.get('User') not in ('',None) or metadata.get('Volumes')):
        raise ValueError('unsupported_api_start_contract')
    env = dict(item.split('=',1) for item in metadata.get('Env',[]) if '=' in item)
    env.update(api.get('environment') or {})
    path=Path(guard_path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('installed_guard_unavailable')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), 'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError('installed_guard_unavailable')
        before=stream.read(65537)
    if len(before)>65536 or hashlib.sha256(before).hexdigest()!=guard_sha256:
        raise ValueError('installed_guard_changed')
    guard=types.ModuleType('firecrawl_candidate_guard')
    exec(compile(before,str(path),'exec'),guard.__dict__)
    for key, expected in guard.EXPECTED_PREDICATES.items():
        name,kind=key.rsplit('_',1)
        actual=env.get(name)=='true' if kind=='true' else bool(env.get(name))
        if actual is not expected: raise ValueError('candidate_configuration')
    if env.get('ALLOW_LOCAL_WEBHOOKS') or env.get('DOTENV_CONFIG_PATH') or env.get('NODE_OPTIONS') or env.get('USE_DB_AUTHENTICATION')!='false':
        raise ValueError('candidate_safeguards')
    # No credentials, volumes, service entrypoint or network in the probe container.
    identity=run(['docker','run','-d','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--memory','256m','--cpus','1','--pids-limit','32',
        '--name','firecrawl-audit-'+uuid.uuid4().hex,'--entrypoint','node','--workdir','/app',
        '--env','USE_DB_AUTHENTICATION=false','--env','PLAYWRIGHT_MICROSERVICE_URL=configured',
        images['api']['image'],'-e','setTimeout(()=>{},180000)']).strip()
    if not re.fullmatch('[0-9a-f]{64}',identity): raise ValueError('candidate_identity')
    # Audit all installed copies in this exact isolated API image, without changing it.
    package_audit_source = "'use strict';\n// This source is embedded in updater-patched.sh; keep the two copies identical.\nconst fs = require('fs'), path = require('path');\nconst required = {'decode-uri-component': [0, 5, 1], systeminformation: [5, 33, 16]};\nconst found = Object.fromEntries(Object.keys(required).map(name => [name, new Map()]));\nconst root = process.argv[1] || '/';\nconst skip = new Set(['/proc', '/sys', '/dev']);\nfunction inspect(dir, expected) {\n  const manifest = path.join(dir, 'package.json');\n  if (!fs.existsSync(manifest)) {\n    if (expected) throw Error('package manifest missing');\n    return;\n  }\n  let data;\n  try { data = JSON.parse(fs.readFileSync(manifest, 'utf8')); }\n  catch { throw Error('invalid package manifest'); }\n  if (expected && data.name !== expected) throw Error('package name mismatch');\n  if (!Object.hasOwn(required, data.name)) return;\n  const match = typeof data.version === 'string' && /^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)$/.exec(data.version);\n  if (!match) throw Error('unknown package version');\n  const actual = match.slice(1).map(BigInt), minimum = required[data.name].map(BigInt);\n  let comparison = 0;\n  for (let i = 0; i < 3 && !comparison; i++) comparison = actual[i] < minimum[i] ? -1 : actual[i] > minimum[i] ? 1 : 0;\n  if (comparison < 0) throw Error('vulnerable package version');\n  found[data.name].set(fs.realpathSync(dir), data.version);\n}\nfunction walk(dir) {\n  if (root === '/' && skip.has(dir)) return;\n  inspect(dir, null);\n  for (const entry of fs.readdirSync(dir, {withFileTypes:true})) {\n    const item = path.join(dir, entry.name);\n    if (entry.isDirectory()) {\n      if (Object.hasOwn(required, entry.name)) inspect(item, entry.name);\n      walk(item);\n    } else if (entry.isSymbolicLink() && Object.hasOwn(required, entry.name)) {\n      const real = fs.realpathSync(item);\n      if (root === '/' && [...skip].some(p => real === p || real.startsWith(p + '/'))) throw Error('unsupported package link');\n      if (!fs.statSync(real).isDirectory()) throw Error('invalid package link');\n      inspect(real, entry.name);\n    }\n  }\n}\nwalk(root);\nconst result = {};\nfor (const [name, copies] of Object.entries(found)) {\n  if (!copies.size) throw Error('required package missing');\n  result[name] = [...copies].sort(([a], [b]) => a.localeCompare(b)).map(([location, version]) => ({location, version}));\n}\nprocess.stdout.write(JSON.stringify(result));\n"
    packages = json.loads(run(['docker','run','--rm','--network','none','--read-only',
        '--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
        '--memory','256m','--cpus','1','--pids-limit','32','--user','0',
        '--entrypoint','node',images['api']['image'],'-e',package_audit_source,'/']))
    report['package_check'] = {'api_image':images['api']['image'], 'packages':packages}
    # Only the inspected container identity changes; every hash/config expectation stays frozen.
    guard.CONTAINER=identity
    result=guard.verify()
    report['transport_check']=result
    report['guard_sha256']=hashlib.sha256(before).hexdigest()
    if path.read_bytes()!=before: raise ValueError('guard_changed')
    # Tags identify upstream releases; the installed guard certifies transport bytes.
    # Reuse only one complete audited profile, never mix files or learn new hashes.
    profile=result.get('transport_profile')
    if (result.get('verified') is not True or profile not in guard.TRANSPORT_PROFILES
            or result.get('receipt_sha256') != guard.TRANSPORT_PROFILES[profile]['receipt_sha256']
            or result.get('api_image') != images['api']['image']
            or result.get('container_id') != identity):
        report['status']='awaiting_transport_review'
        raise ValueError('unadmitted_transport')
    report.update(status='checks_passed',runtime_config_sha256=digest(contract(config)),images=images,
        review_basis={'kind':'installed_reviewed_transport_profile','profile':profile,'receipt_sha256':guard.TRANSPORT_PROFILES[profile]['receipt_sha256']})
    (evidence/'images.json').write_text(json.dumps({'services':images},sort_keys=True))
except Exception:
    # Never include child stderr, environment or exception strings in evidence.
    if report['status']=='checks_passed': report['status']='blocked'
    report['error']='candidate_admission_failed'
finally:
    if identity and re.fullmatch('[0-9a-f]{64}',identity):
        subprocess.run(['docker','rm','-f',identity],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=True)
    (evidence/'checks.json').write_text(json.dumps(report,sort_keys=True))
print(json.dumps({'status':report['status'],'tag':tag,'evidence':str(evidence)}))
if report['status']!='checks_passed': raise SystemExit(1)
PYCANDIDATE
  candidate_checks_sha256=$(sha256sum "$candidate_evidence/checks.json")
  candidate_checks_sha256=${candidate_checks_sha256%% *}
}

verify_candidate_manifest() {
  python3 -B - "$candidate_evidence" "$guard_file" "$guard_sha256" "$latest_commit" "$candidate_checks_sha256" "$preimage" <<'PYMANIFEST'
import hashlib, json, subprocess, sys
from pathlib import Path
root, guard, expected, commit = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
if hashlib.sha256((root/'checks.json').read_bytes()).hexdigest()!=sys.argv[5]: raise SystemExit('Candidate evidence changed')
report=json.loads((root/'checks.json').read_text())
if report.get('status')!='checks_passed' or report.get('source_commit')!=commit: raise SystemExit('Candidate checks missing or stale')
if any(item.is_symlink() for item in (guard, *guard.parents)) or hashlib.sha256(guard.read_bytes()).hexdigest()!=expected or expected!=report['guard_sha256']: raise SystemExit('Reviewed guard changed')
config=json.loads(subprocess.check_output(['docker','compose','config','--format','json'],timeout=30))
for service in config['services'].values(): service.pop('build',None)
if hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=report['runtime_config_sha256']: raise SystemExit('Candidate runtime configuration changed')
images=json.loads((root/'images.json').read_text())
if images!={'services':report['images']}: raise SystemExit('Candidate image manifest changed')
previous=json.loads((Path(sys.argv[6])/'compose-images.json').read_text())['services']
for name in ('nuq-postgres','playwright-service'):
    if name not in previous or previous[name] != images['services'].get(name):
        raise SystemExit('Protected recovery image changed before cutover')
package_check=report.get('package_check')
if (not isinstance(package_check,dict) or package_check.get('api_image')!=report['images']['api']['image']
        or set(package_check.get('packages',{}))!={'decode-uri-component','systeminformation'}
        or any(not copies for copies in package_check['packages'].values())):
    raise SystemExit('Candidate package evidence missing or image changed')
for service in report['images'].values():
    subprocess.run(['docker','image','inspect',service['image']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=10)
# This deploy manifest is only created after checks and the installed reviewed profile agree.
with (root/'manifest.json').open('x') as stream: json.dump(report,stream,sort_keys=True)
PYMANIFEST
}

snapshot_images() {
  # Store only service names and exact image IDs, never rendered environment.
  preimage=$(mktemp -d "$state_dir/preimage.XXXXXXXX")
  docker compose ps --all -q >"$preimage/container-ids"
  python3 -B - "$preimage" <<'PYSNAPSHOT'
import json
from pathlib import Path
import re
import subprocess
import sys
root = Path(sys.argv[1])
ids = (root / "container-ids").read_text().splitlines()
if not ids or len(ids) > 100:
    raise SystemExit("existing Compose image snapshot unavailable")
services = {}
fmt = '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"image":{{json .Image}},"compose_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},"project":{{json (index .Config.Labels "com.docker.compose.project")}}}'
baseline = None
def baseline_files(paths):
    base = [str(Path.cwd() / name) for name in ('docker-compose.yaml', 'docker-compose.override.yaml')]
    if paths[:2] != base:
        raise SystemExit('Unsupported Compose baseline files')
    state = Path('/home/marck/.local/state/firecrawl-updater')
    security = Path('/home/marck/.local/state/firecrawl-security-refresh')
    for name in paths[2:]:
        overlay = Path(name)
        # Image-only overlays accumulate from earlier updater, promotion and
        # security-refresh owners; rollback re-pins every service image below.
        known = ((overlay.parent.parent == state and overlay.parent.name.startswith('candidate.')
                  and (overlay.name == 'images.json' or re.fullmatch(r'images\.candidate-patch-promotion-[0-9a-f]{32}\.json', overlay.name)))
                 or (overlay.parent.parent == state and re.fullmatch(r'brotli-promotion-[0-9a-f]{32}', overlay.parent.name)
                     and overlay.name == 'core-candidate.json')
                 or (overlay.parent.parent == security and re.fullmatch(r'run-[0-9a-f]{32}', overlay.parent.name)
                     and re.fullmatch(r'firecrawl-(?:candidate|recovery)\.json', overlay.name)))
        if not known or any(p.is_symlink() for p in (overlay, *overlay.parents)):
            raise SystemExit('Unsupported Compose image overlay')
        value = json.loads(overlay.read_bytes())
        if (set(value) != {'services'} or not isinstance(value['services'], dict) or not value['services']
                or any(not isinstance(entry,dict) or 'image' not in entry or not set(entry) <= {'image','pull_policy'}
                       or entry.get('pull_policy', 'never') != 'never' or not re.fullmatch(r'sha256:[a-f0-9]{64}',entry['image'])
                       for entry in value['services'].values())):
            raise SystemExit('Compose baseline overlay is not image-only')
    # The exact live images are captured below for every service, including the
    # stopped one-shot init. Historical image-only overlays need not share labels.
    return base
for identity in ids:
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise SystemExit("invalid Compose container identity")
    result = subprocess.run(["docker", "inspect", "--format", fmt, identity], capture_output=True, text=True, timeout=10, check=True)
    data = json.loads(result.stdout)
    service, image = data.get("service"), data.get("image")
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", service) or not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise SystemExit("invalid Compose image snapshot")
    if subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, timeout=10).returncode:
        raise SystemExit("exact Compose recovery image unavailable; refusing cutover")
    if service in services and services[service]["image"] != image:
        raise SystemExit("ambiguous Compose image snapshot")
    files, project = data.get("compose_files"), data.get("project")
    if not isinstance(files, str) or not files or "\n" in files or not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project):
        raise SystemExit("Compose baseline identity unavailable")
    paths = files.split(",")
    if any(not Path(path).is_absolute() or not Path(path).is_file() or Path(path).is_symlink() for path in paths):
        raise SystemExit("Compose baseline file unavailable")
    identity = {"compose_files": baseline_files(paths), "project": project}
    if baseline is not None and baseline != identity:
        raise SystemExit("ambiguous Compose baseline identity")
    baseline = identity
    services[service] = {"image": image, "pull_policy": "never"}
with (root / "compose-images.json").open("x", encoding="utf-8") as stream:
    json.dump({"services": services}, stream, sort_keys=True)
with (root / "compose-baseline.json").open("x", encoding="utf-8") as stream:
    json.dump(baseline, stream, sort_keys=True)
PYSNAPSHOT
}

restore_images() {
  # Refuse before compose if any exact preimage disappeared; do not rebuild or
  # pull a replacement. The protected snapshot remains for owner recovery.
  if ! python3 -B - "$preimage/compose-images.json" "$preimage/compose-baseline.json" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
services = json.load(open(sys.argv[1], encoding="utf-8"))["services"]
for service in services.values():
    subprocess.run(["docker", "image", "inspect", service["image"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
baseline = json.load(open(sys.argv[2], encoding="utf-8"))
arguments = ["docker", "compose", "--project-name", baseline["project"]]
for filename in baseline["compose_files"]:
    path = Path(filename)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise SystemExit("Compose baseline file unavailable")
    arguments.extend(["-f", filename])
arguments.extend(["-f", sys.argv[1], "up", "-d", "--no-build", "--pull", "never", "--remove-orphans"])
subprocess.run(arguments, check=True, timeout=180)
PY
  then
    return 1
  fi
}

rollback() {
  local original_rc=$? failed=0 rolled=0
  trap - ERR
  set +e
  if [[ "$rollback_active" -eq 1 && -n "$previous_commit" ]]; then
    rolled=1
    log "update failed; restoring previous source and exact image preimage"
    if ! git checkout --detach "$previous_commit"; then
      log "rollback failed: previous source could not be restored"
      failed=1
    elif [[ "$deployment_started" -eq 1 ]]; then
      preserve_retired_notice || failed=1
      if ! restore_images; then
        log "rollback failed: exact image preimage could not be restored"
        failed=1
      elif ! smoke_test; then
        log "rollback failed: restored runtime did not pass smoke"
        failed=1
      elif ! set_restart_policy; then
        log "rollback failed: restart policy could not be restored"
        failed=1
      fi
    fi
    preserve_retired_notice || failed=1
    if [[ "$failed" -eq 0 ]]; then
      log "rollback restored previous source and exact image preimage"
    fi
  fi
  failure_notice "$failed" "$rolled"
  exit "$original_rc"
}

failure_notice() {
  # Runs inside rollback with errexit off; a notice never changes the exit status.
  local failed=$1 rolled=$2 detail
  case "$phase" in
    prepare)
      gate block "$state_dir" "$repo" "$latest_tag" "$latest_commit" "$guard_sha256" "${candidate_evidence:--}" \
        "${candidate_dir:--}" "${candidate_base_image:--}"
      gate notify "$state_dir" failed "$latest_tag" "$latest_commit" "${candidate_evidence:--}" \
        "${previous_tag:-$previous_commit}" "candidate build or admission failed" ||
        log "upgrade notice not delivered; retrying next run"
      ;;
    precheck)
      gate notify "$state_dir" cutover_failed "$latest_tag" "$latest_commit" "${candidate_evidence:--}" \
        "${previous_tag:-$previous_commit}" "approved cutover stopped before any change; approval kept" ||
        log "upgrade notice not delivered; retrying next run"
      ;;
    cutover)
      gate retire "$state_dir" failed
      if [[ "$rolled" -eq 1 && "$failed" -eq 0 ]]; then
        detail="cutover failed; previous version restored and smoke passed"
      else
        detail="cutover failed; rollback incomplete, inspect GA401"
      fi
      gate notify "$state_dir" cutover_failed "$latest_tag" "$latest_commit" "${candidate_evidence:--}" \
        "${previous_tag:-$previous_commit}" "$detail" || log "upgrade notice not delivered"
      ;;
  esac
}

# Candidate receipts, single-use approvals, retention and the Owner notice.
# Caller holds run.lock for every mutating subcommand.
gate() {
  FIRECRAWL_NOTICE_REPO=$notice_repo FIRECRAWL_NOTICE_OWNER=$notice_owner python3 -B - "$@" <<'PYGATE'
import datetime as dt, hashlib, json, os, re, secrets, subprocess, sys
from pathlib import Path

TAG = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+')
COMMIT = re.compile(r'[0-9a-f]{40}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
DIGEST = re.compile(r'[0-9a-f]{64}')
NONCE = re.compile(r'[0-9a-f]{32}')
TITLE = '[Firecrawl Upgrade] GA401 core candidate'
UPSTREAM = 'https://github.com/firecrawl/firecrawl'
BINDING = ('tag', 'commit', 'evidence', 'checks_sha256', 'api_image')
command, state, args = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
receipt_path, approval_path = state / 'candidate-ready.json', state / 'approval.json'

def now(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
def sha(data): return hashlib.sha256(data).hexdigest()

def require(pattern, value, what):
    if not isinstance(value, str) or not pattern.fullmatch(value): raise SystemExit('Invalid ' + what)
    return value

def private_dir(path):
    if path.is_symlink(): raise SystemExit('Linked updater state rejected')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077: raise SystemExit('Unsafe updater state directory')
    return path

def read(path):
    if path.is_symlink() or not path.is_file(): raise SystemExit('Updater record unavailable: ' + path.name)
    return path.read_bytes()

def load(path): return json.loads(read(path))

def write(path, value, exclusive=False):
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    target = path if exclusive else path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    if not exclusive: os.replace(target, path)

def move(path, folder, prefix, nonce):
    nonce = nonce if isinstance(nonce, str) and NONCE.fullmatch(nonce) else secrets.token_hex(16)
    target = private_dir(folder) / (prefix + '-' + nonce + '-' + secrets.token_hex(4) + '.json')
    os.rename(path, target)
    return target

def evidence_dir(path):
    root = Path(path)
    if (root.parent != state or not re.fullmatch(r'candidate\.[A-Za-z0-9]{8}', root.name)
            or root.is_symlink() or not root.is_dir()):
        raise SystemExit('Candidate evidence path rejected')
    return root

def admitted(evidence, checks_sha256, tag, commit):
    root = evidence_dir(evidence)
    data = read(root / 'checks.json')
    if sha(data) != checks_sha256: raise SystemExit('Candidate evidence changed')
    report = json.loads(data)
    if report.get('status') != 'checks_passed' or report.get('tag') != tag or report.get('source_commit') != commit:
        raise SystemExit('Candidate checks missing or stale')
    if load(root / 'images.json') != {'services': report['images']}: raise SystemExit('Candidate image manifest changed')
    return report

def record(value, status, **extra):
    folder = private_dir(state / 'candidates')
    evidence = value.get('evidence')
    name = Path(evidence).name if evidence not in (None, '-') else value['tag'] + '-' + value['commit'][:12]
    path = folder / (name + '.json')
    current = load(path) if path.exists() else {}
    current.update(value); current.update(extra, status=status, updated_at=now())
    write(path, current)

def current_receipt():
    if not receipt_path.exists(): return None
    value = load(receipt_path)
    admitted(value['evidence'], value['checks_sha256'], value['tag'], value['commit'])
    return value

def binding(value):
    result = {key: value[key] for key in BINDING}
    result['receipt_sha256'] = sha(read(receipt_path))
    return result

def blocked_key(repo, tag, commit, guard):
    inputs = [tag, commit, guard]
    for name in ('.env', 'docker-compose.override.yaml'):
        path = Path(repo) / name
        inputs.append(sha(path.read_bytes()) if path.is_file() else '-')
    return sha('|'.join(inputs).encode())

def retire(status):
    if receipt_path.exists():
        record(load(receipt_path), status)
        receipt_path.unlink()
    if approval_path.exists():
        move(approval_path, state / 'approvals', status, load(approval_path).get('nonce'))

def gh(method, path, payload=None):
    cmd = [os.environ.get('FIRECRAWL_NOTICE_GH', 'gh'), 'api', '--method', method,
           '-H', 'Accept: application/vnd.github+json', path]
    if payload is not None: cmd += ['--input', '-']
    result = subprocess.run(cmd, input=None if payload is None else json.dumps(payload),
                            capture_output=True, text=True, timeout=60)
    if result.returncode: raise SystemExit('Notice delivery failed: gh api ' + method + ' ' + path)
    return json.loads(result.stdout) if result.stdout.strip() else None

def admission_rows(evidence):
    root = None
    try: root = evidence_dir(evidence)
    except SystemExit: pass
    report = json.loads(read(root / 'checks.json')) if root and (root / 'checks.json').is_file() else {}
    profile = (report.get('review_basis') or {}).get('profile') or (report.get('transport_check') or {}).get('transport_profile')
    packages = ((report.get('package_check') or {}).get('packages')) or {}
    versions = ', '.join(name + ' ' + '/'.join(sorted({copy.get('version', '?') for copy in copies}))
                         for name, copies in sorted(packages.items()))
    checks = sha((root / 'checks.json').read_bytes()) if report else '-'
    api = ((report.get('images') or {}).get('api') or {}).get('image', '-')
    return report.get('status', 'build_failed'), profile or '-', versions or '-', api, checks

def body(event, tag, commit, evidence, previous, detail, owner):
    status, profile, versions, api, checks = admission_rows(evidence)
    compare = (UPSTREAM + '/compare/' + previous + '...' + tag) if TAG.fullmatch(previous or '') else UPSTREAM + '/tree/' + tag
    if event == 'ready':
        lead = ('GA401 Firecrawl core 有新的升級候選 **' + tag + '**，已自動建置並通過 admission，**尚未切換**。'
                '要切換請在 GA401 核准這個 tag（核准只對這個 tag 與這份 evidence 有效、單次使用）：\n\n'
                '```\nfirecrawl-upgrade --approve ' + tag + '\n```\n\n'
                '核准後，下一次每日 `firecrawl-upgrade-candidate.timer` 會切換；要立刻切換，核准後在 GA401 執行 `firecrawl-upgrade`。'
                '出現更新的 tag 或 runtime 設定改變時，舊核准自動作廢。')
    else:
        lead = ('GA401 Firecrawl core 候選 **' + tag + '** 沒有通過自動建置或 admission（`' + status + '`），**不會切換**。'
                '`awaiting_transport_review` 代表 transport 檔案和已審查的 profile 不同，需要先人工 review guard；'
                '其他狀態請看 GA401 的 `journalctl --user -u firecrawl-upgrade-candidate`。同一個 tag、guard 與設定不會每天重建。')
    rows = [('Tag', '`' + tag + '`'), ('Commit', '`' + commit + '`'), ('Upstream compare', compare),
            ('目前執行', '`' + (previous or '-') + '`'), ('Admission', '`' + status + '`（transport profile `' + profile + '`）'),
            ('Package audit', versions), ('切換前 live smoke（現行版本）' if event == 'ready' else '說明', detail or '-'),
            ('API image', '`' + api + '`'),
            ('Checks SHA-256', '`' + checks + '`'), ('Evidence', '`' + evidence + '`')]
    table = '| 項目 | 值 |\n|---|---|\n' + ''.join('| ' + k + ' | ' + v + ' |\n' for k, v in rows)
    return ('@' + owner + ' ' + lead + '\n\n' + table + '\n切換成功後這個 issue 會自動關閉；切換失敗或 rollback 會在這裡留言。'
            '\n\n由 GA401 `firecrawl-upgrade` 發出。')

def find_issue(repo, notice):
    number = notice.get('issue')
    if isinstance(number, int):
        issue = gh('GET', 'repos/' + repo + '/issues/' + str(number))
        if issue.get('state') == 'open' and issue.get('title') == TITLE: return number
    for issue in gh('GET', 'repos/' + repo + '/issues?state=open&per_page=100'):
        if 'pull_request' not in issue and issue.get('title') == TITLE: return issue['number']
    return None

def notify(event, tag, commit, evidence, previous, detail):
    repo = require(re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'), os.environ.get('FIRECRAWL_NOTICE_REPO'), 'notice repository')
    owner = require(re.compile(r'[A-Za-z0-9-]+'), os.environ.get('FIRECRAWL_NOTICE_OWNER'), 'notice owner')
    if event not in ('ready', 'failed', 'approved', 'deployed', 'cutover_failed', 'current'): raise SystemExit('Unknown notice')
    require(TAG, tag, 'tag'); require(COMMIT, commit, 'commit')
    path = state / 'notice.json'
    notice = load(path) if path.exists() else {}
    key = '|'.join((event, tag, commit, evidence, detail))
    if notice.get('last_key') == key: return
    issue = find_issue(repo, notice)
    base = 'repos/' + repo + '/issues'
    comment = None
    if event in ('ready', 'failed'):
        text = body(event, tag, commit, evidence, previous, detail, owner)
        if issue is None:
            issue = gh('POST', base, {'title': TITLE, 'body': text, 'assignees': [owner]})['number']
        else:
            gh('PATCH', base + '/' + str(issue), {'body': text, 'assignees': [owner]})
            comment = ('候選更新為 **' + tag + '**（`' + commit[:12] + '`）：' +
                       ('已通過 admission，等待核准。' if event == 'ready' else '沒有通過，不會切換。') + '詳見上方說明。')
    elif event == 'approved':
        comment = '已核准 **' + tag + '**（`' + commit[:12] + '`）。下一次 `firecrawl-upgrade` 執行時切換。'
    elif event == 'cutover_failed':
        comment = '@' + owner + ' 切換 **' + tag + '** 沒有完成：' + detail + '。核准已用掉或保留的狀態見 `firecrawl-upgrade --status`。'
        if issue is None:
            issue = gh('POST', base, {'title': TITLE, 'body': comment, 'assignees': [owner]})['number']
            comment = None
    elif event == 'deployed':
        comment = ('GA401 core 已切換到 **' + tag + '**（`' + commit[:12] + '`），smoke 通過；' + detail +
                   '。自動關閉。')
    elif event == 'current' and issue is not None:
        comment = 'GA401 core 已是 **' + tag + '**，沒有待核准的候選。自動關閉。'
    if issue is not None and comment:
        gh('POST', base + '/' + str(issue) + '/comments', {'body': comment})
    if issue is not None and event in ('deployed', 'current'):
        gh('PATCH', base + '/' + str(issue), {'state': 'closed', 'state_reason': 'completed'})
    notice.update(last_key=key, issue=issue, event=event, tag=tag, at=now())
    write(path, notice)

def retain(repo, root, live_commit, journals):
    private_dir(state / 'candidates')
    run = lambda *cmd: subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120)
    used = set()
    for identity in run('docker', 'ps', '-aq', '--no-trunc').stdout.split():
        used.add(run('docker', 'inspect', '--format', '{{.Image}}', identity).stdout.strip())
    # Images named by any security journal, recovery preimage or live record stay.
    texts = [p.read_text() for p in Path(journals).glob('run-*/transaction.json') if p.is_file() and not p.is_symlink()]
    texts += [p.read_text() for p in state.glob('preimage.*/compose-images.json') if p.is_file() and not p.is_symlink()]
    texts += [p.read_text() for p in (receipt_path, approval_path) if p.is_file()]
    referenced = '\n'.join(texts)
    live = {Path(root) / ('update-' + live_commit)}
    if receipt_path.is_file(): live.add(Path(load(receipt_path)['candidate_dir']))
    for path in sorted((state / 'candidates').glob('*.json')):
        value = load(path)
        if value.get('cleaned') or value.get('status') not in ('superseded', 'stale', 'failed', 'blocked', 'deployed'): continue
        kept, removed = [], []
        if value['status'] != 'deployed':
            for image in (value.get('api_image'), value.get('base_image')):
                if not isinstance(image, str) or not IMAGE.fullmatch(image): continue
                if run('docker', 'image', 'inspect', image).returncode: continue
                if image in used or image in referenced or run('docker', 'image', 'rm', image).returncode: kept.append(image)
                else: removed.append(image)
        worktree = Path(value.get('candidate_dir') or '-')
        if worktree.parent == Path(root) and re.fullmatch(r'update-[0-9a-f]{40}', worktree.name):
            if worktree in live: kept.append(str(worktree))
            elif worktree.exists():
                if run('git', '-C', repo, 'worktree', 'remove', str(worktree)).returncode: kept.append(str(worktree))
                else: removed.append(str(worktree))
        value.update(cleaned=not kept, retention={'kept': kept, 'removed': removed, 'at': now()})
        write(path, value)
        if removed: print(json.dumps({'retained': path.name, 'removed': removed}))

private_dir(state)
if command == 'status':
    names = ('candidate-ready.json', 'approval.json', 'candidate-blocked.json', 'notice.json')
    print(json.dumps({name: load(state / name) if (state / name).is_file() else None for name in names},
                     indent=2, sort_keys=True))
elif command == 'receipt':
    tag, commit, evidence, checks_sha256, candidate_dir, base_image, smoke = args
    require(TAG, tag, 'tag'); require(COMMIT, commit, 'commit'); require(DIGEST, checks_sha256, 'checks hash')
    if base_image != '-': require(IMAGE, base_image, 'base image')
    report = admitted(evidence, checks_sha256, tag, commit)
    if receipt_path.exists(): raise SystemExit('Another candidate receipt is present')
    value = {'schema': 1, 'tag': tag, 'commit': commit, 'evidence': evidence, 'checks_sha256': checks_sha256,
             'candidate_dir': candidate_dir, 'api_image': report['images']['api']['image'],
             'base_image': None if base_image == '-' else base_image,
             'images': {name: entry['image'] for name, entry in report['images'].items()},
             'transport_profile': report['review_basis']['profile'], 'guard_sha256': report['guard_sha256'],
             'runtime_config_sha256': report['runtime_config_sha256'], 'live_smoke': smoke, 'prepared_at': now()}
    write(receipt_path, value, exclusive=True)
    record(value, 'ready')
elif command == 'ready':
    value = current_receipt()
    sys.exit(0 if value and (value['tag'], value['commit']) == tuple(args) else 3)
elif command == 'field':
    value = current_receipt()
    if value is None: raise SystemExit('No admitted candidate')
    for key in args:
        item = str(value[key])
        if '\n' in item: raise SystemExit('Invalid candidate field')
        print(item)
elif command == 'approve':
    tag, user = args
    if not TAG.fullmatch(tag): raise SystemExit('Approve an exact tag such as v2.11.642')
    value = current_receipt()
    if value is None: raise SystemExit('No admitted candidate is waiting for approval')
    if value['tag'] != tag: raise SystemExit('The admitted candidate is ' + value['tag'] + ', not ' + tag + '; nothing approved')
    bound = binding(value)
    if approval_path.exists():
        existing = load(approval_path)
        if all(existing.get(key) == item for key, item in bound.items()):
            print(json.dumps({'approved': tag, 'commit': value['commit'], 'already': True})); sys.exit(0)
        move(approval_path, state / 'approvals', 'superseded', existing.get('nonce'))
    approval = dict(bound, schema=1, nonce=secrets.token_hex(16), approved_at=now(), approved_by=user)
    write(approval_path, approval, exclusive=True)
    record(value, 'approved', approval_nonce=approval['nonce'])
    print(json.dumps({'approved': tag, 'commit': value['commit']}))
elif command == 'approved':
    tag, commit = args
    if not approval_path.exists(): sys.exit(3)
    approval = load(approval_path)
    try: value = current_receipt()
    except SystemExit: value = None
    if (value is None or (value['tag'], value['commit']) != (tag, commit)
            or any(approval.get(key) != item for key, item in binding(value).items())):
        move(approval_path, state / 'approvals', 'invalid', approval.get('nonce'))
        print('approval does not match the admitted candidate; withdrawn', file=sys.stderr); sys.exit(2)
    for key in ('evidence', 'candidate_dir', 'checks_sha256'): print(value[key])
elif command == 'consume':
    approval = load(approval_path)
    value = current_receipt()
    move(approval_path, state / 'approvals', 'consumed', approval.get('nonce'))
    record(value, 'cutover', approval_nonce=approval.get('nonce'))
elif command == 'retire':
    if args[0] not in ('superseded', 'stale', 'failed', 'deployed'): raise SystemExit('Unknown retirement')
    retire(args[0])
elif command == 'supersede':
    tag, commit = args
    if receipt_path.exists() and (load(receipt_path)['tag'], load(receipt_path)['commit']) != (tag, commit):
        record(load(receipt_path), 'superseded'); receipt_path.unlink()
    if approval_path.exists():
        approval = load(approval_path)
        if (approval.get('tag'), approval.get('commit')) != (tag, commit) or not receipt_path.exists():
            move(approval_path, state / 'approvals', 'superseded', approval.get('nonce'))
elif command == 'config':
    # Exit 1 only for drift; any other failure is distinct so it cannot look like drift.
    try:
        value = current_receipt()
        if value is None: sys.exit(3)
        repo = args[0]; source = Path(value['candidate_dir'])
        if source.name != 'update-' + value['commit'] or not source.is_dir(): raise ValueError()
        compose = ['docker', 'compose', '--project-name', 'firecrawl', '--env-file', repo + '/.env',
                   '-f', str(source / 'docker-compose.yaml'), '-f', repo + '/docker-compose.override.yaml']
        config = json.loads(subprocess.check_output(compose + ['config', '--format', 'json'], timeout=60))
        for service in config['services'].values(): service.pop('build', None)
        current = sha(json.dumps(config, sort_keys=True, separators=(',', ':')).encode())
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 3: raise
        print('candidate configuration check unavailable', file=sys.stderr); sys.exit(4)
    sys.exit(0 if current == value['runtime_config_sha256'] else 1)
elif command == 'block':
    repo, tag, commit, guard, evidence, candidate_dir, base_image = args
    root = None
    try: root = evidence_dir(evidence)
    except SystemExit: pass
    report = json.loads(read(root / 'checks.json')) if root and (root / 'checks.json').is_file() else None
    patched = (root / 'patched-iid').read_text().strip() if root and (root / 'patched-iid').is_file() else None
    value = {'tag': tag, 'commit': commit, 'evidence': evidence, 'candidate_dir': candidate_dir,
             'api_image': patched if patched and IMAGE.fullmatch(patched) else None,
             'base_image': base_image if IMAGE.fullmatch(base_image) else None}
    record(value, 'blocked' if report else 'failed', admission=(report or {}).get('status', 'build_failed'))
    # Only a completed admission verdict waits for review; build failures retry next run.
    if report and report.get('status') in ('awaiting_transport_review', 'blocked'):
        write(state / 'candidate-blocked.json', {'key': blocked_key(repo, tag, commit, guard), 'tag': tag, 'commit': commit,
              'admission': report['status'], 'evidence': evidence, 'recorded_at': now()})
elif command == 'blocked':
    repo, tag, commit, guard = args
    path = state / 'candidate-blocked.json'
    sys.exit(0 if path.is_file() and load(path).get('key') == blocked_key(repo, tag, commit, guard) else 3)
elif command == 'retain':
    retain(*args)
elif command == 'notify':
    notify(*args)
else:
    raise SystemExit('Unknown gate command')
PYGATE
}

open_locks() {
  [[ ! -L "$state_dir" ]] || { log "linked updater state directory rejected"; return 1; }
  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
  # Serialize updater runs separately; preparation shares the transport lock with
  # readers. The installer still needs EX on update.lock and cannot replace us.
  for name in run.lock update.lock; do
    [[ ! -L "$state_dir/$name" ]] || { log "linked updater lock rejected"; return 1; }
  done
  exec 8>"$state_dir/run.lock"
  exec 9>"$state_dir/update.lock"
  python3 -B - "$state_dir/run.lock" "$state_dir/update.lock" <<'PY'
import os
import stat
import sys
for fd, path in zip((8, 9), sys.argv[1:]):
    descriptor, target = os.fstat(fd), os.lstat(path)
    if not stat.S_ISREG(descriptor.st_mode) or not stat.S_ISREG(target.st_mode) or descriptor.st_uid != os.getuid() or descriptor.st_nlink != 1 or (descriptor.st_dev, descriptor.st_ino) != (target.st_dev, target.st_ino):
        raise SystemExit("unsafe updater coordination lock")
    os.fchmod(fd, 0o600)
PY
}

approve_candidate() {
  local tag=$1 output fields
  if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf '%s\n' 'Usage: firecrawl-upgrade --approve vX.Y.Z' >&2
    return 64
  fi
  open_locks
  flock -n 8 || { log "an upgrade run is active; approve again after it finishes"; return 1; }
  gate approve "$state_dir" "$tag" "$(id -un)"
  output=$(gate field "$state_dir" tag commit evidence)
  mapfile -t fields <<<"$output"
  gate notify "$state_dir" approved "${fields[0]}" "${fields[1]}" "${fields[2]}" - "" ||
    log "approval notice not delivered"
  log "approved $tag; the next firecrawl-upgrade run cuts over (run firecrawl-upgrade to start now)"
}

cutover_candidate() {
  phase=precheck
  # Do not interrupt an existing guarded read. Conversion may drop SH on failure;
  # run.lock remains held and we exit without changing live source or containers.
  if ! flock -x -n 9; then
    log "approved cutover deferred: active page reads; approval kept for the next run"
    phase=""
    return
  fi
  # Freeze exact running/stopped service images before any source switch.
  validate_config
  snapshot_images
  # Single use: from here a failure needs a new candidate and a new approval.
  gate consume "$state_dir"
  phase=cutover
  log "updating ${previous_tag:-$previous_commit} to $latest_tag (Owner approved)"
  rollback_active=1
  git checkout --detach "$latest_tag"
  preserve_retired_notice
  validate_config
  verify_candidate_manifest
  deployment_started=1
  docker compose -f "$repo/docker-compose.yaml" -f "$repo/docker-compose.override.yaml" -f "$candidate_evidence/images.json" up -d --no-build --pull never
  smoke_test
  set_restart_policy
  deployment_started=0
  rollback_active=0
  phase=""
  printf 'tag=%s\ncommit=%s\nupdated_at=%s\n' "$latest_tag" "$latest_commit" "$(date --iso-8601=seconds)" >"$state_dir/deployed.env"
  gate retire "$state_dir" deployed
  log "update completed at $latest_tag"
  # The candidate carries its build-time Debian packages. The security owner takes
  # run.lock and update.lock exclusively, so release both before refreshing it.
  exec 8>&- 9>&-
  local security="security refresh failed; core cutover stands" attempt
  # The per-minute recovery timer can hold the owner lock briefly.
  for attempt in 1 2 3; do
    if timeout 100m python3 -B "$security_refresh"; then
      security="security refresh passed"
      break
    fi
    [[ "$attempt" -eq 3 ]] || sleep 30
  done
  [[ "$security" == "security refresh passed" ]] ||
    log "post-cutover security refresh failed; inspect $security_journals/latest.json"
  gate notify "$state_dir" deployed "$latest_tag" "$latest_commit" "$candidate_evidence" \
    "${previous_tag:-$previous_commit}" "$security" || log "upgrade notice not delivered"
}

main() {
  if [[ "$#" -eq 1 && "$1" == --check-only ]]; then
    [[ ! -L "$canonical_state_dir/update.lock" && -f "$canonical_state_dir/update.lock" ]] || return 1
    exec 9<"$canonical_state_dir/update.lock"
    python3 -B - "$canonical_state_dir/update.lock" <<'PYCHECKLOCK'
import os, stat, sys
opened, current = os.fstat(9), os.lstat(sys.argv[1])
if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
    raise SystemExit('unsafe Firecrawl coordination lock')
PYCHECKLOCK
    flock -s -n 9 || { log 'Firecrawl update lock is busy'; return 1; }
    smoke_test
    log "check-only healthy; no update or state changes"
    return
  fi
  if [[ "$#" -eq 2 && "$1" == --approve ]]; then
    approve_candidate "$2"
    return
  fi
  if [[ "$#" -eq 1 && "$1" == --status ]]; then
    gate status "$state_dir"
    return
  fi
  if [[ "$#" -ne 0 ]]; then
    printf '%s\n' 'Usage: firecrawl-upgrade [--check-only | --status | --approve vX.Y.Z]' >&2
    return 64
  fi
  if [[ "$repo" == '/home/marck/github/firecrawl' && "$state_dir" != "$canonical_state_dir" ]]; then
    log "The production Firecrawl checkout requires its canonical coordination lock"
    return 1
  fi
  open_locks
  if ! flock -n 8 || ! flock -s -n 9; then
    log "update or installer already running"
    return
  fi
  trap rollback ERR
  cd "$repo"
  test -f .env
  chmod 600 .env
  if ! validate_worktree; then
    log "tracked worktree changes present; refusing automatic update"
    return 1
  fi
  set_restart_policy
  log "syncing medking82/firecrawl from firecrawl/firecrawl"
  sync_fork_main
  git fetch --tags upstream
  latest_tag=$(git tag --list "${series}.*" --sort=-version:refname | sed -n '1p')
  [[ -n "$latest_tag" ]] || { log "no release tag found for ${series}.x"; return 1; }
  latest_commit=$(git rev-list -n 1 "$latest_tag")
  git merge-base --is-ancestor "$latest_commit" origin/main
  previous_commit=$(git rev-parse HEAD)
  previous_tag=$(git describe --tags --exact-match 2>/dev/null || true)
  # A newer tag invalidates the older candidate and any approval bound to it.
  gate supersede "$state_dir" "$latest_tag" "$latest_commit"
  gate retain "$state_dir" "$repo" "$worktree_root" "$previous_commit" "$security_journals"
  if [[ "$latest_commit" == "$previous_commit" ]]; then
    validate_config
    smoke_test
    set_restart_policy
    printf 'tag=%s\ncommit=%s\nchecked_at=%s\n' "$latest_tag" "$latest_commit" "$(date --iso-8601=seconds)" >"$state_dir/deployed.env"
    gate retire "$state_dir" superseded
    gate notify "$state_dir" current "$latest_tag" "$latest_commit" - "$latest_tag" "" ||
      log "upgrade notice not delivered; retrying next run"
    log "already current at $latest_tag"
    return
  fi
  local approval=0 config=0 output fields
  output=$(gate approved "$state_dir" "$latest_tag" "$latest_commit") || approval=$?
  if [[ "$approval" -eq 0 ]] || gate ready "$state_dir" "$latest_tag" "$latest_commit"; then
    gate config "$state_dir" "$repo" || config=$?
    if [[ "$config" -eq 0 && "$approval" -eq 0 ]]; then
      mapfile -t fields <<<"$output"
      candidate_evidence=${fields[0]} candidate_dir=${fields[1]} candidate_checks_sha256=${fields[2]}
      cutover_candidate
      return
    elif [[ "$config" -eq 0 ]]; then
      gate notify "$state_dir" ready "$latest_tag" "$latest_commit" "$(gate field "$state_dir" evidence)" \
        "${previous_tag:-$previous_commit}" "$(gate field "$state_dir" live_smoke)" ||
        log "upgrade notice not delivered; retrying next run"
      log "candidate $latest_tag ready; awaiting Owner approval (firecrawl-upgrade --approve $latest_tag)"
      return
    elif [[ "$config" -ne 1 ]]; then
      log "candidate configuration check failed"
      return 1
    fi
    log "runtime configuration changed since admission; withdrawing approval and rebuilding the candidate"
    gate retire "$state_dir" stale
  fi
  if gate blocked "$state_dir" "$repo" "$latest_tag" "$latest_commit" "$guard_sha256"; then
    log "candidate $latest_tag already failed admission with this guard and configuration; awaiting review"
    return
  fi
  phase=prepare
  prepare_candidate
  phase=""
  local smoke=passed
  smoke_test || smoke=failed
  gate receipt "$state_dir" "$latest_tag" "$latest_commit" "$candidate_evidence" "$candidate_checks_sha256" \
    "$candidate_dir" "${candidate_base_image:--}" "$smoke"
  gate notify "$state_dir" ready "$latest_tag" "$latest_commit" "$candidate_evidence" \
    "${previous_tag:-$previous_commit}" "$smoke" || log "upgrade notice not delivered; retrying next run"
  log "candidate $latest_tag admitted; awaiting Owner approval (firecrawl-upgrade --approve $latest_tag)"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
