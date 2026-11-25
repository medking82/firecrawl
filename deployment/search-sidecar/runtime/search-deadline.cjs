'use strict';
// Share one 12-second budget across SearXNG pages and DDG retries/body reads.
const {AsyncLocalStorage} = require('node:async_hooks');
const {setTimeout: delay} = require('node:timers/promises');
const Module = require('node:module');
const http = require('node:http');
const fs = require('node:fs');
const {randomUUID} = require('node:crypto');
const contexts = new AsyncLocalStorage();
const workerUrl=process.env.SEARCH_WORKER_URL;
if(workerUrl && workerUrl!=='http://search-worker:8081/v2/search') throw Error('invalid_search_worker_url');
function trace(event,ctx=contexts.getStore(),extra={}) {
  if(process.env.SEARCH_TRACE==='1' && ctx) console.log(JSON.stringify({event,requestId:ctx.id,...extra}));
}
function reason(code) {const e = new Error(code); e.name = 'AbortError'; return e;}
function context(signal,headers={}) {
  const controller = new AbortController();
  const supplied=String(headers['x-search-request-id']||'');
  const id=/^[a-zA-Z0-9_-]{16,64}$/.test(supplied)?supplied:randomUUID().replaceAll('-','');
  const suppliedDeadline=Number(headers['x-search-deadline']);
  const deadline=Math.min(Date.now()+12000,Number.isFinite(suppliedDeadline)&&suppliedDeadline>0?suppliedDeadline:Infinity);
  const timer = setTimeout(() => controller.abort(reason('search_deadline')),Math.max(0,deadline-Date.now()));
  const cancel = () => {trace('search_cancel',ctx);controller.abort(reason('search_cancelled'));};
  const ctx={id,signal:controller.signal,deadline,cancel,finishers:[],closed:false,
    close() {
      if(ctx.closed) return;ctx.closed=true;
      clearTimeout(timer);signal?.removeEventListener('abort',cancel);controller.abort(reason('search_finished'));
      for(const fn of ctx.finishers) fn();
      trace('search_context_closed',ctx,{reason:controller.signal.reason?.message});
    }};
  if (signal) {signal.addEventListener('abort',cancel,{once:true}); if(signal.aborted) cancel();}
  return ctx;
}
function checkpoint() {contexts.getStore()?.signal.throwIfAborted();}
async function sleep(ms) {
  checkpoint();
  const signal=contexts.getStore()?.signal;
  try {return await delay(ms,undefined,{signal});}
  catch(error) {if(signal?.aborted) throw signal.reason;throw error;}
}
async function runSearch(fn,input) {
  if(contexts.getStore()) {checkpoint();return fn(input);}
  const ctx=context(input?.signal);
  try {return await contexts.run(ctx,()=>fn(input));} finally {ctx.close();}
}
function requestBudget() {
  checkpoint();const ctx=contexts.getStore();
  if(!ctx) return {remaining:4000};
  ctx.searxngDeadline ||= Date.now()+6000;
  const remaining=Math.min(ctx.deadline,ctx.searxngDeadline)-Date.now();
  if(remaining<=0) throw reason('searxng_total_deadline');
  return {remaining,signal:ctx.signal};
}
// Preserve the deployed parser and getSecureDispatcher. Reject upstream drift
// rather than silently skipping loop/sleep cancellation on a future core upgrade.
function patchDdg(source) {
  const loop='while (results.length < num_results && nextPageData) {';
  const wait='await new Promise(r => setTimeout(r, 1000));';
  for(const anchor of [loop,wait]) {
    if(source.split(anchor).length!==2) throw Error('unsupported_ddg_deadline_contract');
  }
  return source.replace('"use strict";', '"use strict";\nconst searchDeadline = require('+JSON.stringify(__filename)+');')
    .replace(loop,loop+'\n            searchDeadline.checkpoint();')
    .replace(wait,'await searchDeadline.sleep(1000);');
}
// Core v2 converts exhausted providers and caught exceptions to {}. Validate
// outside that catch so the controller returns an error and continuity can fall back.
async function completeSearch(fn,input) {
  return runSearch(async () => {
    const result = await fn(input);
    checkpoint(); // upstream core can swallow AbortError as well
    const types = ['web','news','images'];
    const present = result && typeof result === 'object' && !Array.isArray(result)
      ? types.filter(key => Object.prototype.hasOwnProperty.call(result,key)) : [];
    if (!present.length || present.some(key => !Array.isArray(result[key]))) {
      throw new Error('search_upstream_unavailable');
    }
    return result;
  },input);
}
async function forwardSearch(input) {
  checkpoint();const ctx=contexts.getStore();
  if(!ctx) throw Error('missing_search_context');
  trace('search_offload_start',ctx);
  const {logger,signal,abortSignal,...payload}=input;
  const body=JSON.stringify(payload);
  const result=await new Promise((resolve,reject)=>{
    const req=http.request(workerUrl,{method:'POST',agent:false,signal:ctx.signal,
      headers:{'Content-Type':'application/json','Content-Length':Buffer.byteLength(body),
        'X-Search-Request-Id':ctx.id,'X-Search-Deadline':String(ctx.deadline)}},res=>{
      const chunks=[];let size=0;
      res.on('data',chunk=>{size+=chunk.length;if(size>1048576) res.destroy(Error('search_worker_response_limit'));else chunks.push(chunk);});
      res.on('error',reject);
      res.on('end',()=>{
        try {
          const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if(res.statusCode!==200 || value.success!==true) throw Error('search_upstream_unavailable');
          resolve(value.data);
        } catch(error) {reject(error);}
      });
    });
    req.on('socket',socket=>{
      socket.once('connect',()=>trace('search_worker_socket',ctx,{localPort:socket.localPort,remotePort:socket.remotePort}));
      socket.once('close',()=>trace('search_worker_socket_closed',ctx));
    });
    req.on('error',error=>reject(ctx.signal.aborted?ctx.signal.reason:error));
    req.end(body);
  });
  checkpoint();return result;
}
const active=()=>Boolean(contexts.getStore());
module.exports={runSearch,completeSearch,checkpoint,sleep,patchDdg,requestBudget,active,forwardSearch};
const isDdg=file=>/\/dist\/src\/search\/v2\/ddgsearch\.js$/.test(file||'');
const isSearch=file=>/\/dist\/src\/search\/(v2\/)?index\.js$/.test(file||'');
const originalJs=Module._extensions['.js'];
Module._extensions['.js']=function(mod,file) {
  if(!isDdg(file)) return originalJs(mod,file);
  return mod._compile(patchDdg(fs.readFileSync(file,'utf8')),file);
};
const load=Module._load, wrapped=new WeakSet();
Module._load=function(name,parent,isMain) {
  const value=load.call(this,name,parent,isMain);
  if(name==='undici' && isDdg(parent?.filename)) {
    return {...value,fetch:function(url,options={}) {
      const ctx=contexts.getStore();
      if(!ctx) {
        if(process.env.SEARCH_WORKER_ROLE==='1') throw Error('missing_search_context');
        return value.fetch(url,options);
      }
      checkpoint();
      let dispatcher=options.dispatcher;
      if(process.env.SEARCH_WORKER_ROLE==='1') {
        if(!ctx.proxyAgent) {
          ctx.proxyAgent=new value.ProxyAgent({uri:'http://search-egress:3128',
            headers:{'x-search-request-id':ctx.id}});
          ctx.finishers.push(()=>ctx.proxyAgent.destroy().then(()=>trace('ddg_dispatcher_closed',ctx),()=>trace('ddg_dispatcher_closed',ctx)));
        }
        dispatcher=ctx.proxyAgent;
      }
      trace('ddg_fetch_start',ctx);
      return value.fetch(url,{...options,dispatcher,signal:options.signal?AbortSignal.any([options.signal,ctx.signal]):ctx.signal});
    }};
  }
  // Resolve first so unrelated (possibly circular) modules are never inspected.
  if(name.startsWith('.') && value && !wrapped.has(value) && /search/.test(name+(parent?.filename||''))
    && isSearch(Module._resolveFilename(name,parent,isMain))
    && Object.prototype.hasOwnProperty.call(value,'search') && typeof value.search==='function') {
    const search=value.search;
    const complete=/\/search\/v2\/index\.js$/.test(Module._resolveFilename(name,parent,isMain))
      ? completeSearch : runSearch;
    let provider=search;
    if(workerUrl) {
      if(complete===completeSearch) provider=forwardSearch;
      else {
        // v1 shares the same isolated provider path but returns SearchResult instances.
        const requireCore=Module.createRequire(Module._resolveFilename(name,parent,isMain));
        const {SearchResult}=requireCore('../../src/lib/entities');
        if(typeof SearchResult!=='function') throw Error('unsupported_v1_search_result_contract');
        provider=async input=>{
          const data=await forwardSearch({...input,sources:['web']});
          if(!Array.isArray(data.web)) throw Error('search_upstream_unavailable');
          return data.web.map(row=>new SearchResult(row.url,row.title,row.description));
        };
      }
    }
    value.search=input=>complete(provider,input);wrapped.add(value);
  }
  return value;
};
// Cancel real API work when its client disconnects, including stalled DDG bodies.
const emit=http.Server.prototype.emit;
http.Server.prototype.emit=function(event,...args) {
  const [req,res]=args;
  if(event!=='request' || !/^\/v[12]\/search(?:\?|$)/.test(req?.url||'')) return emit.call(this,event,...args);
  const ctx=context(undefined,req.headers);
  res.setHeader('X-Search-Request-Id',ctx.id);
  trace('search_request_start',ctx);
  const cancel=()=>{if(!res.writableFinished) ctx.cancel();};
  const cleanup=()=>{req.removeListener('aborted',cancel);res.removeListener('close',cancel);ctx.close();};
  req.once('aborted',cancel);res.once('close',cancel);
  res.once('finish',cleanup);res.once('close',cleanup);
  return contexts.run(ctx,()=>emit.call(this,event,...args));
};
