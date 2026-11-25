'use strict';
// Run the installed Firecrawl parser/provider code without its API/queue startup.
// This container has only the internal search network. All DDG TLS uses the fixed
// CONNECT gateway configured by search-deadline.cjs; no direct fallback is allowed.
const http = require('node:http');
if (process.env.SEARCH_WORKER_ROLE !== '1' || process.env.SEARCH_WORKER_URL) {
  throw new Error('invalid_search_worker_role');
}
const {search} = require('./dist/src/search/v2');
const {ddgSearch} = require('./dist/src/search/v2/ddgsearch');
const {searxng_search} = require('./dist/src/search/v2/searxng');
const {matchesDomains} = require('./dist/src/search/operators');
const {config} = require('./dist/src/config');
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
      const web=inDomains(await searxng_search(input.query,{num_results:input.num_results??5,tbs:input.tbs,
        filter:input.filter,lang:input.lang??'en',country:input.country??'us',location:input.location,safe:input.safe}));
      if(web.length) return {web};
    } catch {
      providerEvent(requestId,'searxng','error');
    }
  }
  return {};
}
const server = http.createServer(async (req,res) => {
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
