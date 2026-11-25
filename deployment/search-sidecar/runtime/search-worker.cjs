'use strict';
// Run the installed Firecrawl parser/provider code without its API/queue startup.
// This container has only the internal search network. All DDG TLS uses the fixed
// CONNECT gateway configured by search-deadline.cjs; no direct fallback is allowed.
const http = require('node:http');
const {randomUUID} = require('node:crypto');
if (process.env.SEARCH_WORKER_ROLE !== '1' || process.env.SEARCH_WORKER_URL) {
  throw new Error('invalid_search_worker_role');
}
const {search} = require('./dist/src/search/v2');
const {ddgSearch} = require('./dist/src/search/v2/ddgsearch');
const {searxng_search} = require('./dist/src/search/v2/searxng');
const {matchesDomains} = require('./dist/src/search/operators');
const {config} = require('./dist/src/config');
const deadline = require('./search-deadline.cjs');
// The core reaches this worker as its SearXNG endpoint. The worker must reach
// the real SearXNG container, never itself.
if (config.SEARXNG_ENDPOINT && !/^http:\/\/searxng:8080\/?$/.test(config.SEARXNG_ENDPOINT)) {
  throw new Error('invalid_searxng_upstream');
}
// DDG answers first. Its budget leaves SearXNG its own 6s share of the 12s
// request deadline; timeouts, errors or repeated empty pages pause DDG briefly
// so a blocked DDG costs one request rather than every request.
const DDG_BUDGET_MS=5000,DDG_PAUSE_MS=120000,DDG_EMPTY_LIMIT=3;
const timedOut=Symbol('ddg_timeout');
let ddgPausedUntil=0,ddgEmpty=0;
function providerEvent(requestId,provider,status,extra={}) {
  console.log(JSON.stringify({event:'search_provider',requestId,provider,status,...extra}));
}
function pauseDdg(requestId,status,started) {
  ddgPausedUntil=Date.now()+DDG_PAUSE_MS;ddgEmpty=0;
  providerEvent(requestId,'ddg',status,{elapsedMs:Date.now()-started,pausedMs:DDG_PAUSE_MS});
}
async function tryDdg(input,requestId,inDomains) {
  if(Date.now()<ddgPausedUntil) {
    providerEvent(requestId,'ddg','paused',{resumeInMs:ddgPausedUntil-Date.now()});return [];
  }
  const started=Date.now();let timer;
  const pending=ddgSearch(input.query,input.num_results??5,{tbs:input.tbs,lang:input.lang??'en',
    country:input.country??'us',proxy:input.proxy,timeout:Math.min(input.timeout??5000,4000)});
  pending.catch(()=>{});
  try {
    const result=await Promise.race([pending,new Promise(resolve=>{timer=setTimeout(()=>resolve(timedOut),DDG_BUDGET_MS);})]);
    if(result===timedOut) {pauseDdg(requestId,'timeout',started);return [];}
    const rows=Array.isArray(result?.web)?result.web:[];
    if(!rows.length) {
      if(++ddgEmpty>=DDG_EMPTY_LIMIT) pauseDdg(requestId,'empty_repeated',started);
      else providerEvent(requestId,'ddg','empty',{elapsedMs:Date.now()-started});
      return [];
    }
    ddgEmpty=0;
    const web=inDomains(result);
    providerEvent(requestId,'ddg',web.length?'ok':'filtered',{count:web.length,elapsedMs:Date.now()-started});
    return web;
  } catch {
    pauseDdg(requestId,'error',started);return [];
  } finally {clearTimeout(timer);}
}
async function orderedSearch(input,requestId) {
  // Fire engine and US-only routing keep the core's own contract.
  if(config.FIRE_ENGINE_BETA_URL || input.usOnly) return search(input);
  const inDomains=result=>(Array.isArray(result?.web)?result.web:[])
    .filter(row=>matchesDomains(row.url,input.includeDomains,input.excludeDomains));
  const ddg=await tryDdg(input,requestId,inDomains);
  if(ddg.length) return {web:ddg};
  if(config.SEARXNG_ENDPOINT) {
    providerEvent(requestId,'searxng','start');
    try {
      const web=inDomains(await searxng_search(input.query,{num_results:input.searxng_results??input.num_results??5,tbs:input.tbs,
        filter:input.filter,lang:input.lang??'en',country:input.country??'us',location:input.location,safe:input.safe}));
      if(web.length) return {web};
    } catch {
      providerEvent(requestId,'searxng','error');
    }
  }
  return {};
}
// SearXNG-compatible GET /search for the core's built-in SearXNG provider. The
// core sends no result count. Page 1 asks DDG for Firecrawl's default 5 rows so
// one DDG page suffices (a second page plus its 1s pause can exceed the DDG
// budget and pause DDG); SearXNG fallback and later pages return up to 10 rows.
// Every request runs under the shared 12s deadline.
const COMPAT_DDG_RESULTS=5,COMPAT_PAGE_RESULTS=10,COMPAT_MAX_PAGE=5;
function compatInput(url) {
  const params=url.searchParams;
  const query=params.get('q');
  const pageno=Number(params.get('pageno')||'1');
  const lang=params.get('language')||'en';
  if(typeof query!=='string' || !query.trim() || query.length>4096 || params.get('format')!=='json'
    || !Number.isInteger(pageno) || pageno<1 || pageno>COMPAT_MAX_PAGE || !/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/.test(lang)) {
    throw new Error('invalid_search_input');
  }
  return {query,pageno,lang,safe:params.get('safesearch')==='2'};
}
async function compatSearch(url,req,res) {
  const {query,pageno,lang,safe}=compatInput(url);
  const requestId=randomUUID().replaceAll('-','');
  const controller=new AbortController();
  const cancel=()=>{if(!res.writableFinished) controller.abort();};
  res.once('close',cancel);
  const input={query,num_results:COMPAT_DDG_RESULTS,searxng_results:COMPAT_PAGE_RESULTS,lang,country:'us',safe,
    signal:controller.signal};
  const started=Date.now();
  try {
    const data=await deadline.runSearch(async value=>{
      if(pageno===1) return orderedSearch(value,requestId);
      providerEvent(requestId,'searxng','start',{pageno});
      const result=await searxng_search(value.query,{num_results:COMPAT_PAGE_RESULTS,lang:value.lang,
        country:value.country,safe:value.safe,page:pageno});
      return {web:Array.isArray(result?.web)?result.web:[]};
    },input);
    const rows=(Array.isArray(data?.web)?data.web:[]).filter(row=>row && typeof row.url==='string')
      .map(row=>({url:row.url,title:String(row.title??''),content:String(row.description??'')}));
    console.log(JSON.stringify({event:'search_compat',requestId,pageno,count:rows.length,elapsedMs:Date.now()-started}));
    return rows;
  } finally {res.removeListener('close',cancel);}
}
function sendJson(res,status,value) {
  if(res.destroyed || res.headersSent) return;
  res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));
}
const server = http.createServer(async (req,res) => {
  if (req.method === 'GET' && /^\/search(?:\?|$)/.test(req.url||'')) {
    try {
      const url=new URL(req.url,'http://search-worker');
      const results=await compatSearch(url,req,res);
      sendJson(res,200,{query:url.searchParams.get('q'),number_of_results:results.length,results});
    } catch(error) {
      const code=['invalid_search_input','search_deadline','search_cancelled'].includes(error?.message)
        ? error.message : 'search_upstream_unavailable';
      console.log(JSON.stringify({event:'search_compat_error',code}));
      sendJson(res,code==='invalid_search_input'?400:code==='search_deadline'?504:502,{error:code});
    }
    return;
  }
  if (req.method !== 'POST' || req.url !== '/v2/search') {
    res.writeHead(404,{'Content-Type':'application/json'});res.end('{"error":"not_found"}');return;
  }
  let size=0;const chunks=[];
  try {
    for await (const chunk of req) {
      size+=chunk.length;
      if(size>16384) throw new Error('invalid_search_input');
      chunks.push(chunk);
    }
    const input=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if(!input || typeof input.query!=='string' || !input.query.trim() || input.query.length>4096) {
      throw new Error('invalid_search_input');
    }
    // Logger methods are process-local and cannot survive the JSON boundary.
    // Ignore a caller-provided logger and emit only provider/status metadata.
    const requestId=res.getHeader('X-Search-Request-Id');
    input.logger={
      info(message){
        const provider=message==='Using searxng search'?'searxng':message==='Using DuckDuckGo search'?'ddg':message==='Using fire engine search'?'fire_engine':'unknown';
        console.log(JSON.stringify({event:'search_provider',requestId,provider}));
      },
      error(){console.log(JSON.stringify({event:'search_provider_error',requestId}));}
    };
    const data=await orderedSearch(input,requestId);
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({success:true,data}));
  } catch(error) {
    if(res.destroyed) return;
    const code=['invalid_search_input','search_deadline','search_cancelled'].includes(error.message)
      ? error.message : 'search_upstream_unavailable';
    res.writeHead(code==='invalid_search_input'?400:code==='search_deadline'?504:500,
      {'Content-Type':'application/json'});
    res.end(JSON.stringify({success:false,error:code}));
  }
});
server.headersTimeout=4000;server.requestTimeout=5000;
server.listen(8081,'0.0.0.0');
