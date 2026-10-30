'use strict';
// Runs inside the candidate image with no network or production mounts.
const assert = require('node:assert/strict');
const Module = require('node:module');
const load = Module._load;
let localCalls = 0, body;
Module._load = function(request, parent, ...rest) {
  if (parent?.filename === '/app/dist/src/lib/hangar.js' && request === '../config')
    return {config:{HANGAR_URL:'http://fixture.invalid'}};
  if (parent?.filename === '/app/dist/src/lib/hangar.js' && request === './local-hangar')
    return {localHangarRequest:async()=>{localCalls++; throw Error('unexpected local call');}};
  return load.call(this,request,parent,...rest);
};
global.fetch = async (_url, init) => {
  body = JSON.parse(init.body);
  return {ok:true,json:async()=>({id:'br_fixture',status:'running',cdp_url:'ws://fixture'})};
};
const hangar = require('/app/dist/src/lib/hangar.js');
const opts = {ttl:30,activityTtl:10,streamWebView:false,recordSession:false,blockAds:false,zeroDataRetention:true};
(async()=>{
  process.env.SELF_HOSTED_BROWSER_SOCKET='/fixture/manager.sock';
  await assert.rejects(hangar.createHangarBrowser('key','team',opts), e=>e.status===400 && /zero.data.retention/i.test(e.message));
  assert.equal(localCalls,0);
  console.log('PASS image_local_zdr_fail_closed');
  delete process.env.SELF_HOSTED_BROWSER_SOCKET;
  await hangar.createHangarBrowser('key','team',opts);
  assert.equal(body.proxy.zero_data_retention,true);
  assert.equal(body.proxy.type,'basic');
  console.log('PASS image_upstream_zdr_proxy_forwarded');
})().catch(e=>{console.error(e);process.exitCode=1;});
