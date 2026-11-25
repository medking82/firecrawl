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
const axios = require('axios');
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
      const web=inDomains(await searxng_search(input.query,{num_results:input.num_results??5,tbs:input.tbs,
        filter:input.filter,lang:input.lang??'en',country:input.country??'us',location:input.location,safe:input.safe}));
      if(web.length) return {web};
    } catch {
      providerEvent(requestId,'searxng','error');
    }
  }
  return {};
}
// SearXNG-compatible GET /search for the core's built-in SearXNG provider. The
// core sends no result count: it assumes 20 rows per page, asks page 1 for
// limit <= 20 and later pages only for larger limits, then trims to limit. So
// every page here returns up to 20 rows. SearXNG pages carry about 7 rows, so
// page p maps to SearXNG pages 3p-2..3p and stops once 20 rows are found. Page
// 1 keeps DDG first (one DDG page, 10 rows). Rows are de-duplicated by URL
// within a page. Every request runs under the shared 12s deadline.
const COMPAT_PAGE_RESULTS=20,COMPAT_DDG_RESULTS=10,COMPAT_MAX_PAGE=5,COMPAT_SEARXNG_PAGES=3;
// Hints the core's SearXNG request cannot carry. The search continuity proxy
// appends them to the query as "fcctl:tbs=..." and "fcctl:country=..."; they
// are removed here before any engine sees the query. Unknown values are ignored.
// country sets DDG's region only: a SearXNG region switches Yahoo to a regional
// host that search-egress does not allow, which suspends the engine.
const HINT=/(^|\s)fcctl:(tbs|country)=(\S{1,96})(?=\s|$)/g;
const DAY_MS=86400000;
function hintDate(match) {
  if(!match) return null;
  const [,month,day,year]=match.map(Number);
  const date=new Date(Date.UTC(year,month-1,day));
  return date.getUTCFullYear()===year && date.getUTCMonth()===month-1 && date.getUTCDate()===day ? date : null;
}
function timeHint(tbs) {
  const parts=String(tbs||'').split(',');
  const qdr=parts.map(part=>/^qdr:([hdwmy])$/.exec(part)).find(Boolean);
  if(qdr) {
    // DDG and SearXNG have no hour filter: an hour widens to a day.
    const unit=qdr[1]==='h'?'d':qdr[1];
    return {ddg:unit,timeRange:{d:'day',w:'week',m:'month',y:'year'}[unit]};
  }
  if(!parts.includes('cdr:1')) return {};
  const pick=name=>hintDate(parts.map(part=>new RegExp('^'+name+':(\\d{1,2})/(\\d{1,2})/(\\d{4})$').exec(part)).find(Boolean));
  const now=new Date(),min=pick('cd_min'),max=pick('cd_max')||now;
  if(!min || max<min) return {};
  const iso=date=>date.toISOString().slice(0,10);
  // SearXNG only knows ranges ending now: use the smallest one reaching cd_min.
  const age=(now-min)/DAY_MS;
  const timeRange=age<=1?'day':age<=7?'week':age<=31?'month':age<=366?'year':undefined;
  return {ddg:iso(min)+'..'+iso(max),timeRange};
}
function takeHints(raw,lang) {
  const found={};
  const query=raw.replace(HINT,(match,lead,name,value)=>{found[name]=value;return lead;}).replace(/\s+/g,' ').trim();
  const country=/^[A-Za-z]{2}$/.test(found.country||'') ? found.country.toLowerCase() : undefined;
  const time=timeHint(found.tbs);
  return {query,country,tbs:time.ddg,timeRange:time.timeRange};
}
async function searxngPage(input,page) {
  // Same target string as searxng-timeout.cjs, so its deadline and logging apply.
  const target=config.SEARXNG_ENDPOINT.replace(/\/$/,'')+'/search';
  const response=await axios.get(target,{headers:{'Content-Type':'application/json'},params:{
    q:input.query,language:input.lang,engines:config.SEARXNG_ENGINES??'',
    categories:config.SEARXNG_CATEGORIES??'',pageno:page,format:'json',
    ...(input.safe?{safesearch:2}:{}),...(input.timeRange?{time_range:input.timeRange}:{})}});
  return (Array.isArray(response.data?.results)?response.data.results:[])
    .map(row=>({url:row.url,title:row.title,description:row.content}));
}
function compatInput(url) {
  const params=url.searchParams;
  const query=params.get('q');
  const pageno=Number(params.get('pageno')||'1');
  const lang=params.get('language')||'en';
  if(typeof query!=='string' || !query.trim() || query.length>4096 || params.get('format')!=='json'
    || !Number.isInteger(pageno) || pageno<1 || pageno>COMPAT_MAX_PAGE || !/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/.test(lang)) {
    throw new Error('invalid_search_input');
  }
  const hints=takeHints(query,lang);
  if(!hints.query) throw new Error('invalid_search_input');
  return {...hints,pageno,lang,safe:params.get('safesearch')==='2'};
}
async function compatPage(input,pageno,requestId) {
  const rows=[],seen=new Set();
  const add=list=>{
    for(const row of Array.isArray(list)?list:[]) {
      if(rows.length>=COMPAT_PAGE_RESULTS) return;
      if(!row || typeof row.url!=='string' || seen.has(row.url)) continue;
      seen.add(row.url);rows.push(row);
    }
  };
  if(pageno===1) add(await tryDdg(input,requestId,result=>Array.isArray(result?.web)?result.web:[]));
  if(!config.SEARXNG_ENDPOINT) return rows;
  const first=(pageno-1)*COMPAT_SEARXNG_PAGES+1;
  for(let page=first;page<first+COMPAT_SEARXNG_PAGES;page++) {
    if(rows.length>=COMPAT_PAGE_RESULTS) break;
    providerEvent(requestId,'searxng','start',{pageno,page});
    try {
      const web=await searxngPage(input,page);
      add(web);
      if(!web.length) break;
    } catch(error) {
      if(['search_deadline','search_cancelled'].includes(error?.message) && !rows.length) throw error;
      providerEvent(requestId,'searxng','error',{page});
      break;
    }
  }
  return rows;
}
async function compatSearch(url,req,res) {
  const {query,pageno,lang,safe,country,tbs,timeRange}=compatInput(url);
  const requestId=randomUUID().replaceAll('-','');
  const controller=new AbortController();
  const cancel=()=>{if(!res.writableFinished) controller.abort();};
  res.once('close',cancel);
  const input={query,num_results:COMPAT_DDG_RESULTS,lang,country:country??'us',tbs,timeRange,safe,
    signal:controller.signal};
  const started=Date.now();
  try {
    const found=await deadline.runSearch(value=>compatPage(value,pageno,requestId),input);
    const rows=found.map(row=>({url:row.url,title:String(row.title??''),content:String(row.description??'')}));
    console.log(JSON.stringify({event:'search_compat',requestId,pageno,count:rows.length,
      hints:{country:country??null,tbs:tbs??null,timeRange:timeRange??null},elapsedMs:Date.now()-started}));
    return {query,rows};
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
      const {query,rows}=await compatSearch(url,req,res);
      sendJson(res,200,{query,number_of_results:rows.length,results:rows});
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
