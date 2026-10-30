"""Disposable, isolated GA401 browser acceptance. Input: reviewed files as JSON on stdin."""
import json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request, uuid
from pathlib import Path

cfg = json.load(sys.stdin)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
ident = uuid.uuid4().hex
prefix = 'fc-accept-' + ident[:12]
network = prefix + '-net'
names = {key: prefix + '-' + key for key in ('db', 'redis', 'rabbit', 'api')}
images = {'db':'sha256:1b3f0c48add7f26ed3a9df806d324c8048226ee75a486f451b08b527d8be8a08',
          'redis':'sha256:978f0e01593e65eed801f2402944efcd936d43b5027e4908a7897baf88ed6241',
          'rabbit':'sha256:e582c0bc7766f3342496d8485efb5a1df782b5ce3886ad017e2eaae442311f69',
          'browser':'sha256:aa50d6e5cfc9cc649a1344433e1c31817b7d6638605bf64505b7c03265700f54',
          'api':cfg.get('api_image','sha256:1786bac1ee0ffd0623b1609e6d8e73dc4956cc1cd5223698b6b274bb5d46a764')}
if images['api'] not in ('sha256:1786bac1ee0ffd0623b1609e6d8e73dc4956cc1cd5223698b6b274bb5d46a764',
                         'sha256:dafa957ae517bb089e6892dd323cd12e9afc471da3ecdae193bed6c6ec21cb7f',
                         'sha256:e97dde38a74efbbbcc487a4d99e90a12bab8f8000c4c7759d6cbeabc1e9a409d'):
    raise ValueError('unreviewed fixture API image')
root = Path(tempfile.mkdtemp(prefix=prefix+'-', dir='/tmp'))
os.chmod(root, 0o755)
manager = None
created = []
network_created = False
state_created = False
volume_created = False
volume = prefix + '-rabbit-data'

def run(args, **kw):
    return subprocess.run(args, check=True, text=True, **kw)
def docker(*args, **kw):
    return run(['docker', *args], **kw)
def start(key, args):
    docker('run','-d','--pull=never','--name',names[key], '--label','firecrawl.acceptance='+ident,
           '--label','com.docker.compose.project='+prefix,'--label','com.docker.compose.service='+key,
           '--network',network,'--network-alias',key, *args, images[key], stdout=subprocess.DEVNULL)
    created.append(names[key])
def container_alive(key):
    status=docker('inspect','--format','{{.State.Running}}',names[key],capture_output=True).stdout.strip()
    if status != 'true':
        log=docker('logs','--tail','30',names[key],capture_output=True)
        raise RuntimeError(key+' exited: '+log.stdout+log.stderr)
    return True
def sql(db, text):
    p = docker('exec','-i',names['db'],'psql','-X','-U','postgres','-d',db,'-v','ON_ERROR_STOP=1','-At',
               input=text,capture_output=True)
    return p.stdout.strip()
def wait(predicate, seconds, label):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if predicate(): return
        time.sleep(.5)
    raise RuntimeError(label+' timeout')
def request(base, path, body=None, method=None, key=None, expected=200):
    headers={'Authorization':'Bearer '+(key or api_key),'Content-Type':'application/json'}
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=60) as response: status,value=response.status,json.load(response)
    except urllib.error.HTTPError as exc: status,value=exc.code,json.load(exc)
    if status != expected: raise RuntimeError(f'{path} status={status} expected={expected} body={value}')
    return value
def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

try:
    # A fixture-only copy returns one static page and never accesses external networks.
    source=cfg['manager']
    first=source.index('  const proxy=http.createServer(')
    last=source.index('  const proxyPath=',first)
    synthetic="""  const proxy=http.createServer((req,res)=>{res.writeHead(200,{'content-type':'text/html'});res.end('<title>Fixture Replay</title><p id=proof>isolated</p>');});
  proxy.on('connect',(_req,client)=>client.end('HTTP/1.1 403 Forbidden\\r\\nContent-Length: 0\\r\\n\\r\\n'));
"""
    source=source[:first]+synthetic+source[last:]
    source=source.replace("}catch(e){json(res,e.status||502,{error:e.status?e.message:'browser_operation_failed'});}",
                          "}catch(e){console.error('fixture_manager_error',e.status||502,e.message);json(res,e.status||502,{error:e.status?e.message:'browser_operation_failed'});}")
    source=source.replace("function execute(s,body) {", "function execute(s,body) { console.error('fixture_execute_shape',typeof body.code,body.language,body.timeout);")
    for filename, content in [('manager.cjs',source),('session.cjs',cfg['session']),('execute.cjs',cfg['execute']),('cdp-pipe.cjs',cfg['pipe'])]:
        (root/filename).write_text(content)
    state=Path('/tmp/firecrawl-browser-fixture-'+ident);state.mkdir(mode=0o700);state_created=True
    marker=state/'.fixture-id';marker.write_text(ident);marker.chmod(0o600)
    cdp_port=free_port()
    env={**os.environ,'BROWSER_STATE_DIR':str(state),'BROWSER_IMAGE':images['browser'],
         'BROWSER_FIXTURE_ID':ident,'BROWSER_FIXTURE_CDP_PORT':str(cdp_port),
         'BROWSER_PUBLIC_WS':f'ws://127.0.0.1:{cdp_port}'}
    manager_log=(root/'manager.log').open('w+')
    manager=subprocess.Popen(['/usr/bin/node',str(root/'manager.cjs')],env=env,stdout=manager_log,stderr=subprocess.STDOUT)
    wait(lambda:(state/'manager.sock').exists() or manager.poll() is not None,15,'manager socket')
    if manager.poll() is not None: raise RuntimeError('manager exited: '+(root/'manager.log').read_text())
    docker('network','create','--internal','--label','firecrawl.acceptance='+ident,network,stdout=subprocess.DEVNULL);network_created=True
    start('db',['--tmpfs','/var/lib/postgresql/data:rw,size=512m','-e','POSTGRES_PASSWORD=fixture','-e','POSTGRES_DB=postgres'])
    def db_ready():
        logs=subprocess.run(['docker','logs',names['db']],capture_output=True,text=True)
        return ('PostgreSQL init process complete' in logs.stdout and
                subprocess.run(['docker','exec',names['db'],'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0)
    wait(db_ready,60,'postgres')
    sql('postgres','CREATE DATABASE browser;')
    sql('postgres',cfg['nuq'])
    for name in ('auth','browser','job','sponsors'): sql('browser',cfg[name])
    sql('browser',cfg['sponsors'])
    team=str(uuid.uuid4());api_key=str(uuid.uuid4());other_team=str(uuid.uuid4());other_key=str(uuid.uuid4())
    sql('browser',f"INSERT INTO teams(id,name,org_id) VALUES ('{team}','fixture',NULL),('{other_team}','fixture other',NULL); INSERT INTO api_keys(key,team_id,name) VALUES ('{api_key}','{team}','fixture'),('{other_key}','{other_team}','fixture'); INSERT INTO blocklist(data,org_id) VALUES ('{{\"blocklist\":[],\"allowedKeywords\":[]}}',NULL); INSERT INTO self_hosted_team_policy(team_id,flags) VALUES ('{team}','{{\"scrapeZDR\":\"allowed\",\"rateLimitOverrides\":{{\"browser\":30,\"browserExecute\":120,\"browserReplay\":60}}}}'),('{other_team}','{{}}');")
    print('PASS isolated_schema_and_fixture_credentials',flush=True)
    start('redis',['--tmpfs','/data:rw,size=64m'])
    docker('volume','create','--label','firecrawl.acceptance='+ident,volume,stdout=subprocess.DEVNULL);volume_created=True
    start('rabbit',['-v',volume+':/var/lib/rabbitmq','-e','ERL_FLAGS=-setcookie fixturecookie',
                    '-e','RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2 +A 4'])
    # The fixture's explicit Erlang cookie lets the broker start without reading
    # host state; AMQP readiness is checked without an Erlang CLI identity.
    wait(lambda:subprocess.run(['docker','exec',names['rabbit'],'bash','-c','exec 3<>/dev/tcp/127.0.0.1/5672'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0,60,'rabbitmq')
    vars={'HOST':'0.0.0.0','PORT':'3002','ENV':'local','USE_DB_AUTHENTICATION':'true',
          'DATABASE_URL':'postgresql://postgres:fixture@db:5432/browser','DISABLE_MONITORING':'true',
          'SELF_HOSTED_ARTIFACT_DIR':'/tmp/artifacts','SELF_HOSTED_BROWSER_SOCKET':'/run/fixture/manager.sock',
          'SELF_HOSTED_BROWSER_LLM_SOCKET':'/run/fixture/llm.sock',
          'HANGAR_URL':'http://fixture-hangar','REDIS_URL':'redis://redis:6379','REDIS_RATE_LIMIT_URL':'redis://redis:6379',
          'NUQ_BACKEND':'pg','NUQ_RABBITMQ_URL':'amqp://rabbit:5672','POSTGRES_HOST':'db','POSTGRES_USER':'postgres',
          'POSTGRES_PASSWORD':'fixture','POSTGRES_DB':'postgres','POSTGRES_PORT':'5432','NUM_WORKERS_PER_QUEUE':'1',
          'NUQ_WORKER_COUNT':'1','CRAWL_CONCURRENT_REQUESTS':'1','MAX_CONCURRENT_JOBS':'1','BROWSER_POOL_SIZE':'1',
          'HARNESS_STARTUP_TIMEOUT_MS':'120000','LOGGING_LEVEL':'info'}
    args=['--mount',f'type=bind,src={state},dst=/run/fixture,readonly','--tmpfs','/tmp:rw,size=256m']
    for key,value in vars.items():args += ['-e',key+'='+value]
    start('api',args)
    container_alive('api')
    networks=json.loads(docker('inspect','--format','{{json .NetworkSettings.Networks}}',names['api'],capture_output=True).stdout)
    ip=networks[network]['IPAddress']
    if not ip:raise RuntimeError('candidate has no fixture network address')
    base='http://'+ip+':3002'
    def ready():
        try:
            with urllib.request.urlopen(base+'/v0/health/readiness',timeout=2) as r:return json.load(r).get('status')=='ok'
        except Exception:return False
    wait(ready,120,'candidate API')
    print('PASS candidate_readiness',flush=True)
    request(base,'/v2/browser',key=str(uuid.uuid4()),expected=401)
    request(base,'/v2/browser',{'ttl':30,'activityTtl':10,'__agentInterop':{'auth':'untrusted','requestId':str(uuid.uuid4()),'shouldBill':True,'hostedMcp':True}},expected=403)
    print('PASS untrusted_hosted_mcp_rejected',flush=True)
    b=request(base,'/v2/browser',{'ttl':180,'activityTtl':120})
    bid=b['id']; cdp=b['cdpUrl']
    assert request(base,'/v2/browser/'+bid)['status']=='running'
    request(base,'/v2/browser/'+bid,key=other_key,expected=403)
    print('PASS create_auth_and_tenant_isolation',flush=True)
    result=request(base,'/v2/browser/'+bid+'/execute',{'code':"await page.setContent('<title>Fixture API</title><p id=proof>persisted</p>')",'language':'node','timeout':20})
    assert result['exitCode']==0,result
    result=request(base,'/v2/browser/'+bid+'/execute',{'code':"console.log(await page.title(),await page.locator('#proof').innerText())",'language':'node','timeout':20})
    assert result['exitCode']==0 and 'Fixture API persisted' in result['stdout'],result
    print('PASS api_execute_persistent_dom',flush=True)
    js="const ws=new WebSocket(process.env.FIXTURE_CDP);ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Browser.getVersion'}));ws.onmessage=e=>{if(!JSON.parse(e.data).result?.product)process.exit(1);console.log('PASS api_cdp_transport');ws.close();process.exit(0)};ws.onerror=()=>process.exit(1);setTimeout(()=>process.exit(2),10000);"
    run(['/usr/bin/node','-e',js],env={**os.environ,'FIXTURE_CDP':cdp},timeout=15)
    stopped=request(base,'/v2/browser/'+bid,method='DELETE');assert stopped['status']=='stopped'
    assert request(base,'/v2/browser/'+bid,method='DELETE')['creditsBilled']==stopped['creditsBilled']
    print('PASS api_delete_idempotent_settlement',flush=True)
    zdr=request(base,'/v2/browser',{'ttl':30,'activityTtl':10,'zeroDataRetention':True},expected=400)
    assert zdr.get('success') is False
    print('PASS api_zdr_fail_closed',flush=True)
    job='019a3000-0000-7000-8000-'+uuid.uuid4().hex[:12]; now=int(time.time()*1000)
    access={'version':1,'teamId':team,'kind':'scrape','expiresAtMs':now+3600000}
    replay={'version':1,'status':'completed','requestId':job,'completedAtMs':now,'creditsBilled':1,
            'replay':{'targetUrl':'http://fixture.invalid/','waitForMs':0,'actions':[]},'browser':{'blockAds':False}}
    sql('browser',f"INSERT INTO self_hosted_browser_jobs(kind,id,value,expires_at) VALUES ('access','{job}','{json.dumps(access)}',now()+interval '1 hour'),('scrape_state','{job}','{json.dumps(replay)}',now()+interval '1 hour');")
    linked='/v2/scrape/'+job+'/interact'
    request(base,linked,{'code':'console.log(1)','language':'node','timeout':10},key=other_key,expected=403)
    try:
        out=request(base,linked,{'code':"console.log(await page.title(),await page.locator('#proof').innerText())",'language':'node','timeout':30})
    except Exception:
        print('fixture manager diagnostics:',(root/'manager.log').read_text()[-3000:],flush=True)
        raise
    assert out['success'] and 'Fixture Replay isolated' in out['stdout'],out
    request(base,linked,method='DELETE')
    print('PASS seeded_scrape_replay_ownership_and_delete',flush=True)
finally:
    if manager is not None:
        manager.terminate()
        try:manager.wait(timeout=15)
        except subprocess.TimeoutExpired:manager.kill();manager.wait()
    # Only the randomly generated fixture label can be removed here.
    sessions=docker('ps','-aq','--filter','label=firecrawl.browser-fixture='+ident,capture_output=True).stdout.split()
    if sessions: docker('rm','-f',*sessions,stdout=subprocess.DEVNULL)
    for name in reversed(created):docker('rm','-f',name,stdout=subprocess.DEVNULL)
    if volume_created:docker('volume','rm',volume,stdout=subprocess.DEVNULL)
    if network_created:docker('network','rm',network,stdout=subprocess.DEVNULL)
    if manager is not None:manager_log.close()
    if state_created:shutil.rmtree(state)
    shutil.rmtree(root)
    print('PASS exact_fixture_cleanup',flush=True)
