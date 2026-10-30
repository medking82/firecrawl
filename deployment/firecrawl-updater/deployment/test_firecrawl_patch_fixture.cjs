'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const {range, integrity, archive, scan, extract} = require('./patch.cjs');

function tar(name, contents, type = '0') {
  const h = Buffer.alloc(512), bytes = Buffer.from(contents);
  h.write(name, 0); h.write('0000644\0', 100); h.write('0000000\0', 108); h.write('0000000\0', 116);
  h.write(bytes.length.toString(8).padStart(11, '0') + '\0', 124); h.write('00000000000\0', 136);
  h.fill(32, 148, 156); h.write(type, 156); h.write('ustar\0', 257);
  h.write([...h].reduce((a,b) => a+b, 0).toString(8).padStart(6, '0') + '\0 ', 148);
  return Buffer.concat([h, bytes, Buffer.alloc((512 - bytes.length % 512) % 512)]);
}
const pack = (...entries) => zlib.gzipSync(Buffer.concat([...entries, Buffer.alloc(1024)]));
test('range checks bounds and rejects unsupported versions', () => {
  const hit = range('>=1.2.3 <2.0.0');
  assert.equal(hit('1.2.3'), true); assert.equal(hit('2.0.0'), false);
  assert.throws(() => hit('1.2.3-beta')); assert.throws(() => range('^1.2.3'));
});
test('lockfile must contain exactly one sha512 resolution in packages', () => {
  const lock = "packages:\n  example@2.0.0:\n    resolution: {integrity: sha512-YWJj}\nsnapshots:\n  example@2.0.0: {}\n";
  assert.equal(integrity(lock, 'example', '2.0.0'), 'sha512-YWJj');
  assert.throws(() => integrity(lock, 'example', '2.1.0'));
  assert.throws(() => integrity(lock.replace('snapshots:', '  example@2.0.0:\n    resolution: {integrity: sha512-YWJj}\nsnapshots:'), 'example', '2.0.0'));
});
test('archive extraction rejects traversal, links, corruption and duplicates', () => {
  const valid = pack(tar('package/package.json', '{"name":"x"}'), tar('package/index.js', 'ok'));
  const files = archive(valid), dir = fs.mkdtempSync(path.join(os.tmpdir(), 'patch-test-'));
  try { extract(files, dir); assert.equal(fs.readFileSync(path.join(dir, 'index.js'), 'utf8'), 'ok'); }
  finally { fs.rmSync(dir, {recursive:true, force:true}); }
  assert.throws(() => archive(pack(tar('package/../bad', 'x'))));
  assert.throws(() => archive(pack(tar('package/link', '', '2'))));
  assert.throws(() => archive(pack(tar('package/package.json', '{}'), tar('package/package.json', '{}'))));
  assert.throws(() => archive(Buffer.from('invalid')));
});
test('enumerates pnpm copies, detects residual versions and rejects other layouts', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'patch-scan-'));
  const pkg = path.join(root, 'node_modules', '.pnpm', 'example@1.2.3', 'node_modules', 'example');
  try {
    fs.mkdirSync(pkg, {recursive:true});
    const write = version => fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({name:'example', version}));
    write('1.2.3'); const hit = range('>=1.0.0 <2.0.0');
    assert.deepEqual(scan(root, 'example', hit).targets, [pkg]);
    const alias = path.join(root, 'alias'); fs.symlinkSync(pkg, alias, 'junction');
    assert.equal(scan(root, 'example', hit).links[0].target, fs.realpathSync(pkg));
    write('2.0.0'); assert.deepEqual(scan(root, 'example', hit).targets, []);
    const other = path.join(root, 'node_modules', 'example'); fs.mkdirSync(other); fs.writeFileSync(path.join(other, 'package.json'), '{"name":"example","version":"1.2.3"}');
    assert.throws(() => scan(root, 'example', hit), /unknown package layout/);
  } finally { fs.rmSync(root, {recursive:true, force:true}); }
});
