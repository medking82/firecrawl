const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../../apps/api/node_modules/typescript');
const {adaptHangar} = require('./hangar-compat.cjs');

const source = fs.readFileSync(path.join(__dirname, '../../apps/api/src/lib/hangar.ts'), 'utf8');
function load(local, optionsFn) {
  const js = ts.transpileModule(adaptHangar(source), {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
  const exports = {};
  const context = {exports, process:{env: local ? {SELF_HOSTED_BROWSER_SOCKET:'/private/socket'} : {}},
    require(id) {
      if (id === '../config') return {config:{HANGAR_URL:'http://hangar'}};
      if (id === './local-hangar') return {localHangarRequest:async()=>{ throw Error('unexpected request'); }};
      if (id === './browser-options') return {hangarBrowserOptions:optionsFn};
      throw Error(id);
    },
    fetch:async (_url, init) => { context.body = JSON.parse(init.body); return {ok:true,json:async()=>({id:'id',status:'running',cdp_url:'ws://test'})}; },
    AbortSignal, URL, Buffer, Date, setTimeout};
  vm.runInNewContext(js, context);
  return {api:exports, context};
}
const opts = {ttl:30, activityTtl:10, streamWebView:false, recordSession:false, blockAds:false};
test('forwards ZDR to upstream options and preserves other fields', async()=>{
  const calls=[];
  const {api,context}=load(false,(options,zdr)=>{calls.push([options,zdr]); return {proxy:{zero_data_retention:zdr}};});
  await api.createHangarBrowser('key','team',{...opts,zeroDataRetention:true});
  assert.equal(calls[0][1],true);
  assert.equal(context.body.proxy.zero_data_retention,true);
  assert.equal(context.body.owner,'team');
});
test('local ZDR fails closed before contacting manager', async()=>{
  const {api}=load(true,()=>{throw Error('unexpected options');});
  await assert.rejects(api.createHangarBrowser('key','team',{...opts,zeroDataRetention:true}),e=>e.status===400 && /zero.data.retention/i.test(e.message));
});
test('ordinary requests pass the non-ZDR flag', async()=>{
  let received;
  const {api}=load(false,(_options,zdr)=>{received=zdr;return {};});
  await api.createHangarBrowser('key','team',{...opts,zeroDataRetention:false});
  assert.equal(received,false);
});
test('unexpected overlay source is rejected',()=>assert.throws(()=>adaptHangar('different source')));
test('v2.11.626 upstream browser options enforce the ZDR proxy', {skip:!process.env.FIRECRAWL_626_OPTIONS_SOURCE}, async()=>{
  const module = {exports:{}};
  const js = ts.transpileModule(process.env.FIRECRAWL_626_OPTIONS_SOURCE, {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
  vm.runInNewContext(js, {exports:module.exports});
  const {api,context}=load(false,module.exports.hangarBrowserOptions);
  await api.createHangarBrowser('key','team',{...opts,zeroDataRetention:true});
  assert.equal(context.body.proxy.type,'basic');
  assert.equal(context.body.proxy.zero_data_retention,true);
});
