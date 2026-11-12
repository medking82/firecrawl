'use strict';
// Qualify the actual compiled candidate exported without runtime configuration.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const audit = process.env.FIRECRAWL_COMPILED_AUDIT;
if (!audit) throw new Error('FIRECRAWL_COMPILED_AUDIT must identify an exported candidate');
const shellFile = path.join(audit, 'src/lib/hosted-bash/shell.js');
const nativeRequire = createRequire(shellFile);
const exportsObject = {};
vm.runInNewContext(fs.readFileSync(shellFile, 'utf8'), {
  exports: exportsObject, __dirname: path.dirname(shellFile), __filename: shellFile,
  require: name => name === './docs' ? { docsFiles: async () => ({}) } : nativeRequire(name),
  Error, Promise, Date, Object, setTimeout, clearTimeout, setInterval, clearInterval,
}, { filename: shellFile });
const caller = { baseUrl: 'http://127.0.0.1:1', ip: '127.0.0.1', authorization: null, headers: {} };
const run = command => exportsObject.runShell(command, caller, new AbortController().signal);

test('real worker runs text pipelines', async () => {
  const result = await run('printf "beta\\nalpha\\n" | sort | head -1');
  assert.equal(result.exitCode, 0);
  assert.equal(result.stdout, 'alpha\n');
});

test('worker cannot read the host filesystem', async () => {
  const result = await run('cat /etc/passwd');
  assert.notEqual(result.exitCode, 0);
  assert.ok(!result.stdout.includes('root:'));
});

test('worker does not inherit parent environment', async () => {
  process.env.FIRECRAWL_QUAL_CANARY = 'qualification-only-not-a-credential';
  try {
    const result = await run('env');
    assert.equal(result.exitCode, 0);
    assert.ok(!result.stdout.includes('FIRECRAWL_QUAL_CANARY'));
    assert.ok(!result.stdout.includes('NODE_PATH'));
  } finally { delete process.env.FIRECRAWL_QUAL_CANARY; }
});

test('network tools and arbitrary language runtimes are unavailable', async () => {
  for (const command of ['curl http://127.0.0.1:1', 'wget http://127.0.0.1:1',
                         'node -e "process.exit(0)"', 'python3 -c "print(1)"']) {
    const result = await run(command);
    assert.notEqual(result.exitCode, 0, command);
  }
});

test('each request receives a fresh virtual filesystem', async () => {
  assert.equal((await run('echo isolated > /qualification-proof')).exitCode, 0);
  assert.notEqual((await run('cat /qualification-proof')).exitCode, 0);
});

test('inline credential flags are rejected without API calls', async () => {
  const result = await run('firecrawl search example --api-key qualification-only');
  assert.notEqual(result.exitCode, 0);
  assert.deepEqual(Array.from(result.endpoints), []);
});

test('aborted requests never start work', async () => {
  const signal = new AbortController(); signal.abort();
  const result = await exportsObject.runShell('echo should-not-run', caller, signal.signal);
  assert.equal(result.exitCode, 130);
  assert.equal(result.stdout, '');
});

test('output is bounded', async () => {
  const result = await run('seq 1 90000');
  assert.ok(result.truncated || (result.exitCode !== 0 && /limit|too large|exceed|max/i.test(result.stderr)),
    JSON.stringify(result));
  assert.ok(Buffer.byteLength(result.stdout) < 257000);
});
