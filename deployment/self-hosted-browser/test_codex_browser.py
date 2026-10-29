import http.client
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import codex_browser as browser
import codex_judge

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('browser_bridge', Path(__file__).with_name('browser-bridge.py'))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def request_value(**changes):
    return {'task': 'Read the result', 'snapshot': '{"text":"untrusted page"}',
            'history': [], 'timeout': 12, **changes}


class BrowserContracts(unittest.TestCase):
    def test_reuses_native_adapter_with_schema_and_remaining_deadline(self):
        decision = {'code': 'console.log(await page.title())', 'done': False, 'summary': 'Read title'}
        result = {'text': json.dumps(decision), 'backend': 'codex-cli', 'model': 'gpt-6.1-sol',
                  'settings': {'model': 'gpt-6.1-sol', 'model_reasoning_effort': 'max', 'service_tier': 'priority'},
                  'tool_calls': 0, 'usage': {'private': 'not logged'}}
        with patch.object(browser, 'judge', return_value=result) as run:
            with patch('sys.stdout', new_callable=io.StringIO) as log:
                self.assertEqual(browser.browser_step(request_value()), decision)
            self.assertEqual(json.loads(log.getvalue()), {key: result[key] for key in (
                'backend', 'model', 'settings', 'tool_calls')})
            self.assertNotIn('untrusted page', log.getvalue())
            self.assertNotIn('Read title', log.getvalue())
            args, kwargs = run.call_args
            self.assertIn('untrusted page', args[1])
            self.assertEqual(kwargs, {'timeout': 12, 'codex_home': None, 'schema': browser.SCHEMA})
            self.assertNotIn('model', kwargs)
            run.assert_called_once()

    def test_rejects_invalid_requests_before_cli(self):
        for changes in [{'timeout': True}, {'timeout': 0}, {'timeout': 111},
                        {'timeout': float('nan')}, {'timeout': float('inf')},
                        {'task': ''}, {'task': '\u20ac' * 14000}, {'snapshot': 4},
                        {'history': 'wrong'}, {'history': ['x'] * 6},
                        {'history': ['x' * 4001]}, {'extra': True}]:
            with self.subTest(changes=list(changes)):
                with patch.object(browser, 'judge') as run:
                    with self.assertRaises(ValueError):
                        browser.browser_step(request_value(**changes))
                    run.assert_not_called()

    def test_valid_completion_and_action(self):
        for value in [{'code': '', 'done': True, 'summary': 'Title: Example'},
                      {'code': 'await page.getByRole("button").click()', 'done': False, 'summary': 'Click'}]:
            self.assertEqual(browser.validate_decision(value), value)

    def test_bad_cli_decisions_fail_closed(self):
        values = [None, [], {}, {'code': '', 'done': True, 'summary': 'ok', 'extra': 1},
                  {'code': 'await page.click("button")', 'done': True, 'summary': 'Not observed'},
                  {'code': '', 'done': False, 'summary': 'No action'},
                  {'code': '', 'done': 1, 'summary': 'Wrong boolean'},
                  {'code': '', 'done': True, 'summary': ''},
                  {'code': 'x' * 20001, 'done': False, 'summary': 'Too big'}]
        for value in values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    browser.validate_decision(value)

    def test_cli_failure_propagates_without_retry(self):
        with patch.object(browser, 'judge', side_effect=TimeoutError('expired')) as run:
            with self.assertRaises(TimeoutError):
                browser.browser_step(request_value())
            run.assert_called_once()

    def test_shared_subprocess_contract_preserves_selection_and_browser_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / 'auth.json').write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"private"}}')
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\nmodel_reasoning_effort="max"\nservice_tier="priority"')
            (home / 'models_cache.json').write_text('{"models":[{"slug":"gpt-6.1-sol"}]}')
            decision = {'code': '', 'done': True, 'summary': 'Observed answer'}

            def spawn(args, **kwargs):
                self.assertEqual(args[0], codex_judge.CLI)
                for setting in ['model="gpt-6.1-sol"', 'model_reasoning_effort="max"',
                                'service_tier="priority"', 'forced_login_method="chatgpt"',
                                'features.shell_tool=false', 'mcp_servers={}']:
                    self.assertIn(setting, args)
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                self.assertNotIn('OPENAI_BASE_URL', kwargs['env'])
                schema_path = Path(args[args.index('--output-schema') + 1])
                self.assertEqual(json.loads(schema_path.read_text()), browser.SCHEMA)
                Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(decision))
                kwargs['stdout'].write(b'{"type":"turn.completed","usage":{}}\n')
                proc = Mock(returncode=0)
                proc.communicate.side_effect = lambda payload, timeout: self.assertEqual(timeout, 12)
                return proc

            with patch.dict(os.environ, {'OPENAI_API_KEY': 'forbidden', 'OPENAI_BASE_URL': 'https://forbidden.invalid'}):
                with patch.object(codex_judge.subprocess, 'Popen', side_effect=spawn) as run:
                    self.assertEqual(browser.browser_step(request_value(), codex_home=home), decision)
                    run.assert_called_once()


class BridgeContracts(unittest.TestCase):
    def setUp(self):
        # Test the real HTTP handler on an ephemeral loopback listener. Production
        # binds only the private Unix socket in browser-bridge.py's entrypoint.
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, value, path='/browser-step', raw=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request('POST', path, value if raw else json.dumps(value))
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_real_http_contract(self):
        answer = {'code': '', 'done': True, 'summary': 'Observed'}
        with patch.object(bridge, 'browser_step', return_value=answer) as step:
            self.assertEqual(self.post(request_value()), (200, answer))
            step.assert_called_once()

    def test_bad_path_input_and_oversize_do_not_invoke_cli(self):
        with patch.object(bridge, 'browser_step') as step:
            self.assertEqual(self.post(request_value(), '/judge')[0], 404)
            self.assertEqual(self.post('{', raw=True)[0], 400)
            self.assertEqual(self.post(request_value(timeout=111))[0], 400)
            # Advertise an oversized body without streaming bytes after the
            # handler has already rejected and closed the connection on Windows.
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
            try:
                connection.putrequest('POST', '/browser-step')
                connection.putheader('Content-Length', '128001')
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 413)
                response.read()
            finally:
                connection.close()
            step.assert_not_called()

    def test_busy_is_immediate_and_failure_releases_slot_without_evidence(self):
        bridge.BUSY.acquire()
        try:
            with patch.object(bridge, 'browser_step') as step:
                self.assertEqual(self.post(request_value())[0], 429)
                step.assert_not_called()
        finally:
            bridge.BUSY.release()
        for error, status in [(RuntimeError('private page'), 502), (TimeoutError('private task'), 504)]:
            with patch.object(bridge, 'browser_step', side_effect=error):
                response = self.post(request_value())
                self.assertEqual(response[0], status)
                self.assertNotIn('private', str(response[1]))
            self.assertTrue(bridge.BUSY.acquire(blocking=False))
            bridge.BUSY.release()


class NodeAgentContracts(unittest.TestCase):
    def test_agent_http_loop_bounds_no_fallback_and_native_entrypoints(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for local agent runtime contracts')
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        completed = subprocess.run([node, '-e', NODE_CONTRACTS, str(ROOT)],
                                   capture_output=True, text=True, timeout=25, **flags)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class NodeJobStoreContracts(unittest.TestCase):
    def run_contracts(self, mode):
        node = shutil.which('node')
        self.assertIsNotNone(node)
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        completed = subprocess.run([node, '-e', NODE_JOB_CONTRACTS, str(ROOT), mode],
                                   capture_output=True, text=True, timeout=30, **flags)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_parameterized_storage_expiry_validation_and_opt_in(self):
        self.run_contracts('memory')

    @unittest.skipUnless(os.environ.get('BROWSER_JOB_TEST_DATABASE_URL'),
                         'No disposable PostgreSQL BROWSER_JOB_TEST_DATABASE_URL supplied')
    def test_disposable_real_postgres(self):
        self.run_contracts('postgres')


NODE_JOB_CONTRACTS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[1], mode = process.argv[2];
const dependencies = path.join(root, 'apps/api/node_modules');
const ts = require(path.join(dependencies,'typescript'));
const drizzle = require(path.join(dependencies,'drizzle-orm'));
const {PgDialect} = require(path.join(dependencies,'drizzle-orm/pg-core'));
const dialect = new PgDialect();
const id='019a3000-0000-7000-8000-000000000001';
const other='019a3000-0000-7000-8000-000000000002';
const team='10000000-0000-4000-8000-000000000001';
let queries=[], failure=null, client=null, created=false;
const memory=new Map();
const key=(kind,id)=>kind+':'+id;
const db={execute:async value=>{
  const query=dialect.sqlToQuery(value); queries.push(query);
  if(failure) throw failure;
  if(client) return client.query(query.sql,query.params);
  if(query.sql.includes('INSERT INTO')) {
    const [kind,id,value,expiry]=query.params;
    memory.set(key(kind,id),{value:JSON.parse(value),expiry:Date.parse(expiry)});
    return {rows:[]};
  }
  const [kind,id]=query.params, row=memory.get(key(kind,id));
  return {rows: row && row.expiry>Date.now() ? [{value:row.value}] : []};
}};
const source=name=>fs.readFileSync(path.join(root,'apps/api/src/lib',name+'.ts'),'utf8');
function load(name, deps) {
  const module={exports:{}};
  const output=ts.transpileModule(source(name),{compilerOptions:{target:ts.ScriptTarget.ES2022,
    module:ts.ModuleKind.CommonJS,esModuleInterop:true},reportDiagnostics:true});
  assert.equal((output.diagnostics||[]).filter(x=>x.category===ts.DiagnosticCategory.Error).length,0);
  vm.runInNewContext(output.outputText,{module,exports:module.exports,Buffer,process,console,
    require:id=>id in deps?deps[id]:require(id)});
  return module.exports;
}
const local=load('local-browser-job-store',{'drizzle-orm':drizzle,'../db/connection':{db}});
const config={};
let bigtableCalls=0;
const common={
  '../config':{config}, './local-browser-job-store':local,
  './bigtable-client':{getBigtableTable:()=>{bigtableCalls++;throw Error('Hosted fallback forbidden');}},
  './bigtable-row-key':{saltedUuidV7RowKey:id=>id},
  './otel-tracer':{setSpanAttributes:()=>{},withSpan:(_name,run)=>run({})},
};
const options=load('browser-options',{});
const access=load('job-access-store',{...common,'./keyless-team':{persistedTeamId:id=>id}});
const state=load('job-state-store',{...common,'./browser-options':options});
const operational=load('operational-job-access',{
  './job-access-store':access,
  './bigtable-row-key':{isUuidV7Id:id=>/^[0-9a-f]{8}-[0-9a-f]{4}-7/.test(id)},
  './logger':{logger:{error:()=>{}}},
});
const mutate=async(kind,id,value,expiry=Date.now()+60000)=>{
  if(client) await client.query('UPDATE public.self_hosted_browser_jobs SET value=$1::jsonb,expires_at=$2 WHERE kind=$3 AND id=$4::uuid',
    [JSON.stringify(value),new Date(expiry),kind,id]);
  else memory.set(key(kind,id),{value,expiry});
};
(async()=>{
  if(mode==='postgres') {
    const {Client}=require(path.join(dependencies,'pg'));
    client=new Client({connectionString:process.env.BROWSER_JOB_TEST_DATABASE_URL});
    await client.connect();
    const name=(await client.query('SELECT current_database() AS name')).rows[0].name;
    assert(name.startsWith('firecrawl_browser_test'), 'Use a disposable firecrawl_browser_test* database only');
    const existing=await client.query("SELECT to_regclass('public.self_hosted_browser_jobs') AS existing");
    assert.equal(existing.rows[0].existing,null,'Refusing to touch an existing job store');
    await client.query(fs.readFileSync(path.join(root,'deployment/self-hosted-browser/002-job-store.sql'),'utf8'));
    created=true;
  }
  delete process.env.SELF_HOSTED_BROWSER_SOCKET;
  process.env.SELF_HOSTED_BROWSER_LLM_SOCKET='/only-llm';
  process.env.SELF_HOSTED_ARTIFACT_DIR='/only-monitor-artifacts';
  const params={id,teamId:team,kind:'scrape',expiresAt:new Date(Date.now()+60000)};
  assert.equal(await access.writeApiJobAccess(params),false);
  assert.equal(await access.readApiJobAccess(id),null);
  const replay={targetUrl:'https://fixture.test/?token=untrusted',waitForMs:10,actions:[]};
  const terminal={status:'completed',requestId:id,completedAtMs:Date.now(),creditsBilled:2,
    replay,browser:{blockAds:true,proxy:'basic'}};
  assert.equal(await state.writeScrapeJobState(id,terminal),false);
  assert.equal(await state.readScrapeJobState(id),null);
  assert.equal(queries.length,0);
  process.env.SELF_HOSTED_BROWSER_SOCKET='/isolated-browser';
  assert.equal(await access.writeApiJobAccess(params),true);
  const stored=await access.readApiJobAccess(id);
  assert.equal(stored.teamId,team); assert.equal(stored.kind,'scrape');
  assert.equal((await operational.getScrapeJobAccess(id)).teamId,team);
  assert.equal(await state.writeScrapeJobState(id,terminal),true);
  // JSONB canonicalizes object key order. Compare values across VM realms.
  assert.deepEqual(JSON.parse(JSON.stringify(await state.readScrapeJobState(id))),terminal);
  const insert=queries.find(q=>q.sql.includes('INSERT INTO'));
  assert(insert.sql.includes('$1')&&insert.sql.includes('$2::uuid')&&insert.sql.includes('$3::jsonb'));
  assert(!insert.sql.includes(team)); assert(!insert.sql.includes(id));
  assert.equal(insert.params[0],'access');
  // Composite key separates state from ownership, and tenants never overwrite
  // each other via a shared team bucket. The native controller checks teamId.
  await access.writeApiJobAccess({...params,id:other,teamId:'20000000-0000-4000-8000-000000000002'});
  assert.equal((await access.readApiJobAccess(id)).teamId,team);
  assert.notEqual((await access.readApiJobAccess(other)).teamId,team);
  // Same key is updated, never duplicated; wrong access kinds stay inaccessible.
  await access.writeApiJobAccess({...params,kind:'agent'});
  assert.equal(await operational.getScrapeJobAccess(id),null);
  await access.writeApiJobAccess(params);
  await mutate('access',id,{...stored,expiresAtMs:Date.now()-1});
  assert.equal(await access.readApiJobAccess(id),null);
  await access.writeApiJobAccess(params);
  await mutate('access',id,stored,Date.now()-1);
  assert.equal(await access.readApiJobAccess(id),null);
  await state.writeScrapeJobState(id,{...terminal,completedAtMs:Date.now()-86400001});
  assert.equal(await state.readScrapeJobState(id),null);
  // Even if the SQL expiry is accidentally extended, validate embedded age.
  await mutate('scrape_state',id,{version:1,...terminal,completedAtMs:Date.now()-86400001});
  assert.equal(await state.readScrapeJobState(id),null);
  await state.writeScrapeJobState(id,{...terminal,error:'x'.repeat(20000)});
  assert.equal((await state.readScrapeJobState(id)).error.length,16384);
  await state.writeScrapeJobState(id,{status:'completed',requestId:id,completedAtMs:Date.now(),
    creditsBilled:2,zeroDataRetention:true});
  const zdr=await state.readScrapeJobState(id);
  assert.equal(zdr.zeroDataRetention,true); assert.equal(zdr.replay,undefined);
  for(const [kind,read] of [['access',access.readApiJobAccess],['scrape_state',state.readScrapeJobState]]) {
    await mutate(kind,id,{version:99});
    await assert.rejects(read(id),/Invalid Bigtable/);
  }
  await mutate('scrape_state',id,{version:1,...terminal,browser:{blockAds:'bad'}});
  await assert.rejects(state.readScrapeJobState(id),/Invalid Bigtable/);
  failure=Error('Database outage');
  await assert.rejects(access.readApiJobAccess(id),/Database outage/);
  await assert.rejects(access.writeApiJobAccess(params),/Database outage/);
  await assert.rejects(state.readScrapeJobState(id),/Database outage/);
  await assert.rejects(state.writeScrapeJobState(id,terminal),/Database outage/);
  failure=null;
  assert.equal(bigtableCalls,0);
  assert.equal(await state.writeExtractJobState(other,{status:'completed',completedAtMs:Date.now(),creditsBilled:1}),false);
  console.log('Job store contracts passed ('+mode+')');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(client) {
    if(created) await client.query('DROP TABLE public.self_hosted_browser_jobs');
    await client.end();
  }
});
'''


NODE_CONTRACTS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');
const root = process.argv[1];
const ts = require(path.join(root, 'apps/api/node_modules/typescript'));
const source = name => fs.readFileSync(path.join(root, 'apps/api/src/lib', name), 'utf8');
let calls = [], bodies = [], answers = [], mode = '', count = 0, cloudCalls = 0;
const fakePage = {
  url: () => 'https://fixture.test/', title: async () => 'Fixture',
  locator: selector => ({
    innerText: async () => count ? 'Clicked: 1' : 'Click to start',
    evaluateAll: async () => [{tag:'BUTTON', text:'Start'}],
  }),
  getByRole: () => ({click: async () => { count++; }}),
};
const executeHangarBrowser = async (id, options) => {
  assert.equal(id, 'br_fixture'); assert.equal(options.language, 'node');
  assert(options.timeout >= 1 && options.timeout <= 120);
  calls.push(options);
  if (mode === 'execution-error' && !options.code.includes('controls:'))
    return {stdout:'private page',stderr:'secret exception',exitCode:1,killed:false};
  const outputs = [];
  const execute = new (Object.getPrototypeOf(async function(){}).constructor)(
    'page', 'context', 'browser', 'console', options.code);
  await execute(fakePage, {}, {}, {log: value => outputs.push(String(value))});
  return {stdout:outputs.join('\n'),stderr:'',exitCode:0,killed:false};
};
function load(text, customRequire) {
  const module = {exports:{}};
  const compiled = ts.transpileModule(text, {compilerOptions:{target:ts.ScriptTarget.ES2022,
    module:ts.ModuleKind.CommonJS, esModuleInterop:true}, reportDiagnostics:true});
  assert.equal((compiled.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error).length, 0);
  vm.runInNewContext(compiled.outputText, {module,exports:module.exports,require:customRequire,
    Buffer,process,setTimeout,clearTimeout,__dirname:path.join(root,'apps/api/src/lib/scrape-interact')});
  return module.exports;
}
const agent = load(source('local-browser-agent.ts'), id => id === './hangar' ? {executeHangarBrowser} : require(id));
const cloud = () => {cloudCalls++; throw Error('Cloud must not run');};
const native = load(source('scrape-interact/browser-agent.ts'), id => {
  if (id === '../local-browser-agent') return agent;
  if (id === '../hangar') return {executeHangarBrowser};
  if (id === '../../config') return {config:{IS_PRODUCTION:false}};
  if (id === '../logger') return {logger:{}};
  if (id === '../generic-ai') return {getModel:cloud};
  if (id === 'ai' || id === './langsmith') return {generateText:cloud};
  if (id === 'zod') return {z:{}};
  return require(id);
});
const socket = process.platform === 'win32' ? '\\\\.\\pipe\\codex-browser-contract-' + process.pid
  : path.join(require('node:os').tmpdir(), 'codex-browser-contract-' + process.pid + '.sock');
const server = http.createServer((req,res) => {
  assert.equal(req.url, '/browser-step'); assert.equal(req.method, 'POST');
  let body = '';
  req.on('data', chunk => body += chunk);
  req.on('end', () => {
    const parsed = JSON.parse(body); bodies.push(parsed);
    assert(parsed.timeout > 0 && parsed.timeout <= 110);
    assert.equal(parsed.task, 'Click Start');
    const reply = () => {
      if (mode === 'http-error') {res.writeHead(502); res.end('{"error":"private"}'); return;}
      if (mode === 'oversize') {res.end('x'.repeat(65000)); return;}
      res.end(JSON.stringify(answers.shift() || {code:'console.log("step")',done:false,summary:'Continue'}));
    };
    if (mode === 'slow') setTimeout(reply, 1500); else reply();
  });
});
const reset = () => {calls=[]; bodies=[]; answers=[]; mode=''; count=0;};
(async () => {
  await new Promise(resolve => server.listen(socket, resolve));
  process.env.SELF_HOSTED_BROWSER_LLM_SOCKET = socket;
  await native.selectBrowserAgentTab('br_fixture'); assert.equal(calls.length, 0);
  answers = [
    {code:'await page.getByRole("button", {name:"Start"}).click(); console.log("clicked")',done:false,summary:'Click Start'},
    {code:'',done:true,summary:'Clicked Start once'},
  ];
  let result = await native.executePromptViaBrowserAgent('Click Start','br_fixture',10,{});
  assert.equal(result.exitCode,0,JSON.stringify({result,calls,bodies,count})); assert.equal(result.output,'Clicked Start once');
  assert.equal(count,1); assert.equal(calls.length,3); assert.equal(bodies.length,2);
  assert(bodies[1].snapshot.includes('Clicked: 1'));
  assert(bodies[1].history[0].includes('clicked')); assert.equal(cloudCalls,0);
  reset();
  result = await agent.executeLocalBrowserAgent('Click Start','br_fixture',10);
  assert.equal(result.exitCode,1); assert.match(result.stderr,/step limit/);
  assert.equal(bodies.length,5); assert.equal(calls.length,9);
  for (const bad of [null,{code:'console.log("bad")',done:true,summary:'Unobserved'},
    {code:'',done:false,summary:'No action'}, {code:'',done:true,summary:'ok',extra:1}]) {
    reset(); answers=[bad];
    // null is deliberately sent separately rather than interpreted as a default.
    if (bad === null) answers=[{code:4,done:true,summary:'bad'}];
    result = await agent.executeLocalBrowserAgent('Click Start','br_fixture',10);
    assert.equal(result.exitCode,1); assert.equal(calls.length,1);
  }
  for (const failure of ['http-error','oversize','execution-error']) {
    reset(); mode=failure;
    result = await agent.executeLocalBrowserAgent('Click Start','br_fixture',10);
    assert.equal(result.exitCode,1); assert(!result.stderr.includes('private'));
    assert(!result.stderr.includes('secret')); assert.equal(bodies.length,1);
  }
  reset(); mode='slow';
  const started=Date.now();
  result=await agent.executeLocalBrowserAgent('Click Start','br_fixture',0.3);
  // Very small budgets fail before issuing the 1-second-minimum supervisor call.
  assert.equal(result.exitCode,1); assert(Date.now()-started<1000);
  reset(); mode='slow';
  // The shared deadline, not the number of model calls, bounds the whole loop.
  const deadlineStarted=Date.now();
  result=await agent.executeLocalBrowserAgent('Click Start','br_fixture',1.1);
  assert.equal(result.exitCode,1); assert.equal(bodies.length,1);
  assert.equal(result.killed,true); assert(Date.now()-deadlineStarted<1400);
  reset(); process.env.SELF_HOSTED_BROWSER_LLM_SOCKET = socket + '-missing';
  result=await native.executePromptViaBrowserAgent('Click Start','br_fixture',3,{});
  assert.equal(result.exitCode,1); assert.equal(cloudCalls,0);
  console.log('Node agent contracts passed');
})().catch(error => {console.error(error); process.exitCode=1;})
  .finally(() => server.close());
'''


if __name__ == '__main__':
    unittest.main()
