// Exercise the merged judge with a real Unix socket and isolated cloud boundaries.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const ts = require('typescript');

const sourceRoot = path.resolve(__dirname, '../../apps/api/src');
const logger = { warn() {}, error() {} };
const changed = {
  meaningful: true, confidence: 'high', reason: 'Price changed',
  meaningfulChanges: [{ type: 'changed', before: '$19', after: '$24', reason: 'Price changed' }],
};
const contradictory = { ...changed, meaningfulChanges: [] };
const args = { logger, goal: 'Track price', markdownDiff: { diffText: '-$19\n+$24' } };

function load(file, env, imports) {
  const source = fs.readFileSync(path.join(sourceRoot, file), 'utf8');
  const result = ts.transpileModule(source, {
    fileName: file, reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  });
  assert.deepEqual((result.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error), []);
  const exports = {};
  vm.runInNewContext(result.outputText, {
    exports, process: { env }, Buffer, Error, AbortController, setTimeout, clearTimeout,
    require(name) {
      if (Object.hasOwn(imports, name)) return imports[name];
      if (name === 'node:http') return require(name);
      throw new Error(`Unexpected dependency: ${name}`);
    },
  }, { filename: file });
  return exports;
}

function judge(env, generateText) {
  const local = load('lib/local-monitor-judge.ts', env, {});
  return load('services/monitoring/judgeChange.ts', env, {
    ai: { generateText },
    '../../lib/local-monitor-judge': local,
    '../../lib/ai-usage-telemetry': { withUsageTelemetry: x => x },
    '../../lib/otel-tracer': { isZeroDataRetentionActive: () => false },
    '../../scraper/scrapeURL/error': { UsOnlyUnavailableError: class extends Error {} },
    './search/tuning': {
      googleModel: () => ({}),
      googleProviderOptions: functionId => ({ functionId }),
      monitorTelemetryMetadata: feature => ({ feature }),
    },
  }).judgeChange;
}

async function socketJudge(t, replies) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'judge-'));
  const socketPath = path.join(root, 'judge.sock');
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    requests.push(JSON.parse(body));
    const reply = replies[Math.min(requests.length - 1, replies.length - 1)];
    res.statusCode = reply.status || 200;
    res.end(JSON.stringify({ text: JSON.stringify(reply.verdict) }));
  });
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, resolve);
  });
  return { requests, env: { SELF_HOSTED_JUDGE_SOCKET: socketPath } };
}

function forbidCloud() { assert.fail('Local judge must not call the cloud provider'); }

test('local socket preserves a valid verdict without cloud calls', async t => {
  const local = await socketJudge(t, [{ verdict: changed }]);
  const result = await judge(local.env, forbidCloud)(args);
  assert.equal(result.meaningfulChanges[0].after, '$24');
  assert.equal(local.requests.length, 1);
});

test('local contradictory verdict is retried with original diff and validation instructions', async t => {
  const local = await socketJudge(t, [{ verdict: contradictory }, { verdict: changed }]);
  const result = await judge(local.env, forbidCloud)(args);
  assert.equal(result.meaningfulChanges.length, 1);
  assert.equal(local.requests.length, 2);
  assert.ok(local.requests[1].prompt.startsWith(local.requests[0].prompt));
  assert.match(local.requests[1].prompt, /previous JSON verdict was invalid/);
});

test('persistent local contradiction stops after one validation retry', async t => {
  const local = await socketJudge(t, [{ verdict: contradictory }]);
  const result = await judge(local.env, forbidCloud)(args);
  assert.equal(local.requests.length, 2);
  assert.equal(result.meaningful, true);
  assert.equal(result.meaningfulChanges.length, 0);
});

test('local HTTP error fails open without switching to a cloud provider', async t => {
  const local = await socketJudge(t, [{ status: 503 }]);
  const result = await judge(local.env, forbidCloud)(args);
  assert.equal(local.requests.length, 1);
  assert.equal(result.meaningful, true);
  assert.equal(result.confidence, 'low');
  assert.match(result.reason, /HTTP 503/);
});

test('cloud branch keeps distinct telemetry for validation retry', async () => {
  const calls = [];
  const result = await judge({}, async options => {
    calls.push(options);
    return { text: JSON.stringify(calls.length === 1 ? contradictory : changed) };
  })(args);
  assert.equal(result.meaningfulChanges.length, 1);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].experimental_telemetry.functionId, 'monitor/judgeChange');
  assert.equal(calls[1].experimental_telemetry.functionId, 'monitor/judgeChangeValidationRetry');
  assert.equal(calls[1].functionId, 'judgeChangeValidationRetry');
});
