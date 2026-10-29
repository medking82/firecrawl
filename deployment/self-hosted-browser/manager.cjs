// Trusted lifecycle supervisor. Caller code is NEVER evaluated in this process.
const http = require('node:http');
const net = require('node:net');
const dns = require('node:dns').promises;
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, execFileSync } = require('node:child_process');
const ROOT = process.env.BROWSER_STATE_DIR;
const RUNTIME = __dirname;
const sessions = new Map();
const keys = new Map();
const LIMIT = 2;
function persist() {
  const temp=path.join(ROOT,'sessions.tmp');
  fs.writeFileSync(temp,JSON.stringify({sessions:[...sessions.values()].map(({busy,...s})=>s),keys:[...keys]}),{mode:0o600});
  fs.renameSync(temp,path.join(ROOT,'sessions.json'));
}
function publicIP(ip) {
  if (net.isIP(ip) !== 4) return false;
  const [a,b,c] = ip.split('.').map(Number);
  return !(a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 168 || b === 0 || (b === 88 && c === 99))) ||
    (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || (a === 203 && b === 0 && c === 113));
}
async function address(host) {
  const rows = await dns.lookup(host, { family: 4, all: true });
  if (!rows.length || rows.some(r => !publicIP(r.address))) throw new Error('private_destination_blocked');
  return rows[0].address; // Pin the checked address, never resolve again at connect.
}
function pair(a,b) {
  a.on('error', () => b.destroy()); b.on('error', () => a.destroy());
  a.pipe(b).pipe(a);
}
function json(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, {'Content-Type':'application/json','Content-Length':Buffer.byteLength(text)}); res.end(text);
}
function docker(args) { return execFileSync('/usr/bin/docker', args, {encoding:'utf8',timeout:40000}).trim(); }
function view(s) { return {id:s.id,status:s.status,created_at:s.created,ended_at:s.ended || null,max_expires_at:s.expires,recording:false}; }
function stop(s) {
  if (s.status === 'stopped') return;
  try{docker(['rm','-f',s.name]);}catch(e){if(docker(['ps','-aq','--filter','name=^/'+s.name+'$']))throw e;}
  s.status='stopped'; s.ended=Date.now()/1000;
  // Exact owned directory: only socket entries; no traversal of caller files.
  if(fs.existsSync(s.dir)){
    for (const name of fs.readdirSync(s.dir)) fs.unlinkSync(path.join(s.dir,name));
    fs.rmdirSync(s.dir);
  }
  persist();
}
function socketRequest(s, endpoint) {
  return Promise.resolve(JSON.parse(docker(['exec',s.name,'node','-e',`fetch('http://127.0.0.1:9222${endpoint}').then(r=>r.text()).then(console.log).catch(()=>process.exit(1))`])));
}
async function create(body,key) {
  // Activation owns this marker through the final idle check and restart.
  if(fs.existsSync(path.join(ROOT,'admission-paused')))
    throw Object.assign(new Error('session_admission_paused'),{status:503});
  if (!key || typeof body.owner !== 'string') throw Object.assign(new Error('owner_and_idempotency_required'),{status:400});
  const previous=keys.get(body.owner+':'+key);
  if (previous) {
    const s=sessions.get(previous),deadline=Date.now()+30000;
    while(s.status==='starting'&&Date.now()<deadline)await new Promise(r=>setTimeout(r,100));
    if(s.status==='starting')throw Object.assign(new Error('creation_pending'),{status:409});
    return s;
  }
  if (body.profile || body.proxy || body.recording?.enabled || body.live_view?.enabled || body.extensions?.length)
    throw Object.assign(new Error('unsupported_browser_option'),{status:422});
  if ([...sessions.values()].filter(s=>s.status==='running'||s.status==='starting').length>=LIMIT)
    throw Object.assign(new Error('session_capacity'),{status:429});
  const ttl=body.max_lifetime_seconds, idle=body.idle_timeout_seconds;
  if (!Number.isInteger(ttl)||ttl<30||ttl>3600||!Number.isInteger(idle)||idle<10||idle>3600)
    throw Object.assign(new Error('invalid_ttl'),{status:400});
  const id='br_'+crypto.randomUUID().replaceAll('-','');
  const now=Date.now()/1000;
  const s={id,name:'firecrawl-browser-session-'+id,dir:path.join(ROOT,'sessions',id),status:'starting',created:now,expires:now+ttl,last:now,idle,token:crypto.randomBytes(32).toString('hex')};
  sessions.set(id,s); keys.set(body.owner+':'+key,id);
  fs.mkdirSync(s.dir,{mode:0o700});
  persist();
  try {
    docker(['run','-d','--name',s.name,'--label','firecrawl.self-hosted-browser=session','--pull=never',
      '--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
      '--pids-limit=256','--memory=1g','--cpus=1','--shm-size=256m',
      '--tmpfs','/tmp:rw,nosuid,nodev,size=384m,uid=1000,gid=1000','--user','1000:1000',
      '--mount',`type=bind,src=${RUNTIME},dst=/opt/firecrawl-browser,readonly`,
      '--mount',`type=bind,src=${ROOT}/egress,dst=/run/egress,readonly`,
      '--workdir','/usr/src/app',process.env.BROWSER_IMAGE,'node','/opt/firecrawl-browser/session.cjs']);
    const deadline=Date.now()+28000;
    while (Date.now()<deadline) {
      try {
        const version=await socketRequest(s,'/json/version');
        const suffix=new URL(version.webSocketDebuggerUrl).pathname;
        s.cdp_url=`${process.env.BROWSER_PUBLIC_WS}/cdp/${s.token}${suffix}`;s.status='running';persist(); return s;
      } catch { await new Promise(r=>setTimeout(r,250)); }
    }
    throw new Error('browser_start_timeout');
  } catch(e) { try{stop(s);}catch{} throw e; }
}
function execute(s,body) {
  if (body.language !== 'node' || typeof body.code !== 'string' || Buffer.byteLength(body.code)>100000 || !Number.isInteger(body.timeout)||body.timeout<1||body.timeout>300)
    throw Object.assign(new Error('node_code_and_bounded_timeout_required'),{status:400});
  if(s.busy) throw Object.assign(new Error('session_busy'),{status:409});
  s.busy=true;s.last=Date.now()/1000;
  return new Promise(resolve=>{
    // timeout lives INSIDE the container; killing docker exec alone leaks its child.
    const p=spawn('/usr/bin/docker',['exec','-i',s.name,'timeout','--signal=KILL',String(body.timeout),'node','/opt/firecrawl-browser/execute.cjs']);
    let stdout='',stderr='',truncated=false;
    const append=(old,chunk)=>{if(old.length+chunk.length>64000)truncated=true;return (old+chunk).slice(0,64000);};
    p.stdout.on('data',x=>stdout=append(stdout,x));p.stderr.on('data',x=>stderr=append(stderr,x));
    p.on('error',()=>{stderr='execution_unavailable';});
    p.on('close',code=>{s.busy=false;s.last=Date.now()/1000;resolve({stdout,stderr,result:stdout,exit_code:code ?? 1,killed:code===124||code===137,truncated});});
    p.stdin.on('error',()=>{});p.stdin.end(body.code);
  });
}
// The HTTP upgrade transport is independently testable without starting Docker sessions.
function createCdpServer(sessionMap = sessions, spawnProcess = spawn) {
  const cdp=http.createServer((req,res)=>json(res,404,{error:'not_found'}));
  cdp.on('upgrade',(req,client,head)=>{
    let upstream,closed=false,killTimer;
    const closeTransport=()=>{
      if(closed)return;
      closed=true;
      client.destroy();
      if(!upstream)return;
      client.unpipe(upstream.stdin);
      upstream.stdout.unpipe(client);
      upstream.stdout.destroy();
      // EOF ends only cdp-pipe.cjs, never the browser/session container.
      upstream.stdin.end();
      if(upstream.exitCode===null&&upstream.signalCode===null){
        killTimer=setTimeout(()=>upstream.kill(),1000);
        killTimer.unref();
      }
    };
    // Install before authentication or piping: a tunnel can reset at any point.
    client.on('error',closeTransport);
    client.once('close',closeTransport);
    client.once('end',closeTransport);
    const m=req.url.match(/^\/cdp\/([a-f0-9]{64})(\/devtools\/browser\/[a-f0-9-]+)$/);
    const s=m&&[...sessionMap.values()].find(s=>s.token===m[1]&&s.status==='running');
    if(!s){client.end('HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n');return;}
    upstream=spawnProcess('/usr/bin/docker',['exec','-i',s.name,'node','/opt/firecrawl-browser/cdp-pipe.cjs']);
    upstream.stdin.on('error',closeTransport);
    upstream.stdout.on('error',closeTransport);
    upstream.on('error',closeTransport);
    upstream.once('close',()=>{clearTimeout(killTimer);closeTransport();});
    s.last=Date.now()/1000;
    upstream.stdin.write(`GET ${m[2]} HTTP/1.1\r\n`+Object.entries(req.headers).map(([k,v])=>`${k}: ${k==='host'?'localhost:9222':v}`).join('\r\n')+'\r\n\r\n');
    if(head.length)upstream.stdin.write(head);
    client.pipe(upstream.stdin);upstream.stdout.pipe(client);
    client.on('data',()=>s.last=Date.now()/1000);
  });
  return cdp;
}
async function main() {
  fs.mkdirSync(path.join(ROOT,'sessions'),{recursive:true,mode:0o700});
  fs.mkdirSync(path.join(ROOT,'egress'),{recursive:true,mode:0o700});
  if(fs.existsSync(path.join(ROOT,'sessions.json'))){
    const saved=JSON.parse(fs.readFileSync(path.join(ROOT,'sessions.json'),'utf8'));
    for(const s of saved.sessions){
      if(!/^br_[a-f0-9]{32}$/.test(s.id))throw new Error('invalid_saved_session');
      s.dir=path.join(ROOT,'sessions',s.id);s.name='firecrawl-browser-session-'+s.id;
      sessions.set(s.id,s);
    }
    for(const [k,v]of saved.keys)if(sessions.has(v))keys.set(k,v);
    // A restart ends in-flight sessions rather than resurrecting stale CDP URLs.
    for(const s of sessions.values())if(s.status!=='stopped')stop(s);
  }
  // Recover only our labelled disposable containers after an unclean supervisor exit.
  const orphans=docker(['ps','-aq','--filter','label=firecrawl.self-hosted-browser=session']).split(/\s+/).filter(Boolean);
  if(orphans.length)docker(['rm','-f',...orphans]);
  for(const name of fs.readdirSync(path.join(ROOT,'sessions'))) {
    if(!/^br_[a-f0-9]{32}$/.test(name))throw new Error('unexpected_session_directory');
    const dir=path.join(ROOT,'sessions',name);
    for(const entry of fs.readdirSync(dir))fs.unlinkSync(path.join(dir,entry));fs.rmdirSync(dir);
  }
  const proxy=http.createServer(async(req,res)=>{
    try {
      const u=new URL(req.url);if(u.protocol!=='http:'||(u.port&&u.port!=='80')||u.username||u.password)throw new Error();
      const ip=await address(u.hostname);
      const headers={...req.headers,host:u.host};delete headers['proxy-authorization'];delete headers['proxy-connection'];
      const upstream=http.request({host:ip,port:80,path:u.pathname+u.search,method:req.method,headers},reply=>{res.writeHead(reply.statusCode,reply.headers);reply.pipe(res);});
      upstream.setTimeout(30000,()=>upstream.destroy());upstream.on('error',()=>res.destroy());req.pipe(upstream);
    }catch{res.writeHead(403);res.end('destination_blocked');}
  });
  proxy.on('connect',async(req,client,head)=>{
    try {
      const u=new URL('https://'+req.url);if((u.port&&u.port!=='443')||u.username||u.password)throw new Error();
      const ip=await address(u.hostname);const upstream=net.connect({host:ip,port:443},()=>{
        client.write('HTTP/1.1 200 Connection Established\r\n\r\n');if(head.length)upstream.write(head);pair(client,upstream);
      });upstream.setTimeout(120000,()=>upstream.destroy());upstream.on('error',()=>client.destroy());
    }catch{client.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');}
  });
  const proxyPath=path.join(ROOT,'egress','proxy.sock');if(fs.existsSync(proxyPath))fs.unlinkSync(proxyPath);
  proxy.listen(proxyPath,()=>fs.chmodSync(proxyPath,0o600));
  const server=http.createServer(async(req,res)=>{
    try {
      let raw='';for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>128000)throw Object.assign(new Error('input_limit'),{status:413});}
      const body=raw?JSON.parse(raw):{};const url=new URL(req.url,'http://localhost');
      if(req.method==='POST'&&url.pathname==='/v1/browsers') {
        const s=await create(body,req.headers['idempotency-key']);return json(res,200,{...view(s),cdp_url:s.cdp_url});
      }
      const m=url.pathname.match(/^\/v1\/browsers\/(br_[a-f0-9]{32})(?:\/(stop|execute))?$/);
      const s=m&&sessions.get(m[1]);if(!s)return json(res,404,{error:'not_found'});
      if(req.method==='GET'&&!m[2])return json(res,200,view(s));
      if(req.method==='POST'&&m[2]==='stop'){stop(s);return json(res,200,view(s));}
      if(req.method==='POST'&&m[2]==='execute'){
        if(s.status!=='running')return json(res,410,{error:'session_stopped'});
        return json(res,200,await execute(s,body));
      }
      json(res,404,{error:'not_found'});
    }catch(e){json(res,e.status||502,{error:e.status?e.message:'browser_operation_failed'});}
  });
  const sock=path.join(ROOT,'manager.sock');if(fs.existsSync(sock))fs.unlinkSync(sock);
  server.listen(sock,()=>fs.chmodSync(sock,0o600));
  const cdp=createCdpServer();
  cdp.listen(3008,'127.0.0.1');
  setInterval(()=>{
    const now=Date.now()/1000;
    for(const s of sessions.values()) {
      if(s.status==='running'&&(now>=s.expires||(!s.busy&&now-s.last>=s.idle)))try{stop(s);}catch{}
      // Keep small stopped receipts so native DB settlement survives long downtime.
    }
  },1000).unref();
  process.on('SIGTERM',()=>{for(const s of sessions.values())try{stop(s);}catch{} process.exit(0);});
}
if(require.main===module)main().catch(e=>{console.error(e.message);process.exit(1);});
module.exports={publicIP,createCdpServer,create};
