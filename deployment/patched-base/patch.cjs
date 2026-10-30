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
  if (!/^[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9_.-]+$/.test(base || '') || !/^sha256:[a-f0-9]{64}$/.test(baseId || '')) fail('verified local base tag and image ID required');
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
