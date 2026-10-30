"""Install a pinned release into a separate GA401 Compose project. Never edits Firecrawl production."""
import concurrent.futures,hashlib,json,os,secrets,subprocess,time,uuid,urllib.request
from pathlib import Path

HERE=Path(__file__).resolve().parent
SOURCE=HERE.parents[1]
ROOT=Path('/home/marck/services/firecrawl-monitor')
STATE=ROOT/'shared'
PROJECT='firecrawl-monitor'
ENVFILE=STATE/'runtime.env'

def run(args,**kwargs):
    return subprocess.run(args,check=True,text=True,**kwargs)
def output(args): return subprocess.check_output(args,text=True).strip()
def compose(*args,**kwargs):
    return run(['docker','compose','--project-name',PROJECT,'--env-file',str(ENVFILE),'-f',str(HERE/'compose.yaml'),*args],**kwargs)
def psql(text):
    return compose('exec','-T','app-db','psql','-X','-U','postgres','-d','monitor','-v','ON_ERROR_STOP=1',input=text)
def atomic_private(path,text):
    temp=path.with_suffix('.tmp')
    temp.write_text(text); temp.chmod(0o600); temp.replace(path)

os.umask(0o077)
STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
baseline={}
production=Path('/home/marck/github/firecrawl')
for name in ['.env','docker-compose.yaml','docker-compose.override.yaml']:
    p=production/name
    baseline[name]=hashlib.sha256(p.read_bytes()).hexdigest()
baseline['containers']={n:json.loads(output(['docker','inspect',n]))[0]['State']['StartedAt'] for n in ['firecrawl-api-1','firecrawl-redis-1','firecrawl-rabbitmq-1','firecrawl-nuq-postgres-1','firecrawl-playwright-service-1','firecrawl-foundationdb-1']}
atomic_private(STATE/'production-before.json',json.dumps(baseline,indent=2))

if not ENVFILE.exists():
    if output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+PROJECT]):
        raise RuntimeError('Unowned existing monitor project; stop before touching it')
    images={k:json.loads(output(['docker','inspect',n]))[0]['Image'] for k,n in {
        'BASE_API':'firecrawl-api-1','POSTGRES_IMAGE':'firecrawl-nuq-postgres-1',
        'RABBIT_IMAGE':'firecrawl-rabbitmq-1','REDIS_IMAGE':'firecrawl-redis-1'}.items()}
    config={**images,'APP_DB_PASSWORD':secrets.token_hex(32),'MONITOR_IMAGE':'firecrawl-monitor:'+HERE.parents[1].name,
            'JUDGE_SOCKET_DIR':str(STATE/'judge')}
    atomic_private(ENVFILE,'\n'.join(f'{k}={v}' for k,v in config.items())+'\n')
else:
    config=dict(line.split('=',1) for line in ENVFILE.read_text().splitlines() if '=' in line)
    config['MONITOR_IMAGE']='firecrawl-monitor:'+HERE.parents[1].name
    atomic_private(ENVFILE,'\n'.join(f'{k}={v}' for k,v in config.items())+'\n')

# Existing cached images only. No mutable tag, registry pull, dependencies or prod rebuild.
base_tag='firecrawl-monitor-base:'+config['BASE_API'].split(':')[-1]
run(['docker','tag',config['BASE_API'],base_tag])
assert output(['docker','image','inspect',base_tag,'--format','{{.Id}}'])==config['BASE_API']
run(['docker','build','--network=none','--pull=false','--build-arg','BASE_API='+base_tag,
     '-t',config['MONITOR_IMAGE'],'-f',str(HERE/'Dockerfile'),str(SOURCE)])
compose('up','-d','app-db','redis','rabbitmq')
for _ in range(30):
    check=subprocess.run(['docker','exec','firecrawl-monitor-app-db-1','pg_isready','-U','postgres','-d','monitor'],capture_output=True)
    if check.returncode==0:break
    time.sleep(1)
else: raise RuntimeError('Isolated PostgreSQL readiness timeout')
# The cached image's init SQL may not have run (root-only source permissions).
# Apply the pinned queue schema explicitly to its own postgres DB before workers.
compose('exec','-T','app-db','psql','-X','-U','postgres','-d','postgres','-v','ON_ERROR_STOP=1',
        input=(SOURCE/'apps/nuq-postgres/nuq.sql').read_text())
psql((HERE/'001-monitor.sql').read_text())
psql((HERE/'002-artifact-gc.sql').read_text())
psql((HERE/'003-agent-sponsors.sql').read_text())
clients=STATE/'client.json'
if not clients.exists():
    test_sql=(HERE/'test-db.sql').read_text()
    def phase(name):
        command=['docker','compose','--project-name',PROJECT,'--env-file',str(ENVFILE),'-f',str(HERE/'compose.yaml'),
            'exec','-T','app-db','psql','-X','-U','postgres','-d','monitor','-v','ON_ERROR_STOP=1','-v','phase='+name]
        result=run(command,input=test_sql,capture_output=True)
        print('DB contract phase passed:',name,flush=True)
        return result.stdout
    phase('contract')
    phase('setup')
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(phase,name) for name in ['worker_a','worker_b']]
            for job in jobs: job.result()
        phase('verify')
    finally: phase('cleanup')
    client={'api_url':'http://127.0.0.1:13006','remote_url':'http://127.0.0.1:3006','team_id':str(uuid.uuid4()),'api_key':str(uuid.uuid4())}
    atomic_private(clients,json.dumps(client))
client=json.loads(clients.read_text())
# UUID-generated values only; no user text interpolated into SQL.
team=str(uuid.UUID(client['team_id'])); key=str(uuid.UUID(client['api_key']))
psql(f"INSERT INTO public.teams(id,name,org_id) VALUES ('{team}','GA401 self-hosted monitor',NULL) ON CONFLICT(id) DO NOTHING;\n"
     f"INSERT INTO public.api_keys(key,team_id,name) VALUES ('{key}','{team}','local monitor') ON CONFLICT(key) DO NOTHING;\n"
     "INSERT INTO public.blocklist(data,org_id) SELECT '{\"blocklist\":[],\"allowedKeywords\":[]}'::jsonb,NULL WHERE NOT EXISTS(SELECT 1 FROM public.blocklist WHERE org_id IS NULL);\n")
psql(f"INSERT INTO public.self_hosted_team_policy(team_id,flags) VALUES ('{team}','{{\"rateLimitOverrides\":{{\"crawl\":30,\"crawlStatus\":120}}}}'::jsonb) ON CONFLICT(team_id) DO NOTHING;\n")

socket_dir=STATE/'judge'; socket_dir.mkdir(exist_ok=True,mode=0o700)
unit=Path('/home/marck/.config/systemd/user/firecrawl-monitor-judge.service')
unit.write_text(f'''[Unit]
Description=Firecrawl Monitor Private CLI Judge
After=opencodex-proxy.service
[Service]
Type=simple
ExecStart=/usr/bin/python3 -B {HERE}/judge-bridge.py
Environment=MONITOR_JUDGE_SOCKET={socket_dir}/judge.sock
UMask=0077
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=default.target
''')
run(['systemctl','--user','daemon-reload'])
run(['systemctl','--user','enable','--now','firecrawl-monitor-judge.service'])
run(['systemctl','--user','restart','firecrawl-monitor-judge.service'])
compose('up','-d','api')
ready=False
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:3006/v0/health/readiness',timeout=2) as r:
            if json.load(r).get('status')=='ok':ready=True;break
    except Exception:pass
    time.sleep(1)
if not ready:
    compose('stop','api')
    raise RuntimeError('Monitor API readiness timeout; isolated API stopped, production remains unchanged')
prune_unit=Path('/home/marck/.config/systemd/user/firecrawl-monitor-prune.service')
prune_unit.write_text('''[Unit]
Description=Firecrawl Monitor Retention Pruning
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec firecrawl-monitor-api-1 node /app/monitor-prune.cjs
TimeoutStartSec=120
NoNewPrivileges=true
''')
Path('/home/marck/.config/systemd/user/firecrawl-monitor-prune.timer').write_text('''[Unit]
Description=Hourly Firecrawl Monitor Retention Pruning
[Timer]
OnCalendar=hourly
RandomizedDelaySec=120
Persistent=true
Unit=firecrawl-monitor-prune.service
[Install]
WantedBy=timers.target
''')
run(['systemctl','--user','daemon-reload'])
run(['systemctl','--user','enable','--now','firecrawl-monitor-prune.timer'])
for name,expected in baseline.items():
    if name=='containers':continue
    assert hashlib.sha256((production/name).read_bytes()).hexdigest()==expected, 'Production config changed: '+name
for name,started in baseline['containers'].items():
    assert json.loads(output(['docker','inspect',name]))[0]['State']['StartedAt']==started, 'Production container restarted: '+name
print(json.dumps({'status':'ready','monitor_url':'http://127.0.0.1:3006','project':PROJECT,'production_unchanged':True,'release':str(SOURCE)}))
