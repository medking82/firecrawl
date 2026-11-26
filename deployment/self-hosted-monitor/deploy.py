"""Install a pinned release into a separate GA401 Compose project. Never edits Firecrawl production."""
import concurrent.futures,hashlib,json,os,secrets,subprocess,sys,time,uuid,urllib.request
from pathlib import Path

HERE=Path(__file__).resolve().parent
SOURCE=HERE.parents[1]
sys.path.insert(0, str(SOURCE / 'deployment'))
from extension_release import install_images, lock_installation, select_release, verify_project_images

ROOT=Path.home()/'services/firecrawl-monitor'
STATE=ROOT/'shared'
PROJECT='firecrawl-monitor'
ENVFILE=STATE/'runtime.env'

def run(args,**kwargs):
    return subprocess.run(args,check=True,text=True,**kwargs)
def output(args): return subprocess.check_output(args,text=True).strip()
def compose(*args,**kwargs):
    return run(['docker','compose','--project-name',PROJECT,'--env-file',str(ENVFILE),'-f',str(HERE/'compose.yaml'),*args],env=compose_env,**kwargs)
def psql(text):
    return compose('exec','-T','app-db','psql','-X','-U','postgres','-d','monitor','-v','ON_ERROR_STOP=1',input=text)
def atomic_private(path,text):
    temp=path.with_suffix('.tmp')
    temp.write_text(text); temp.chmod(0o600); temp.replace(path)

release = select_release(SOURCE, ENVFILE, 'monitor', (
    'deploy.py', 'compose.yaml', '001-monitor.sql', '002-artifact-gc.sql',
    '003-agent-sponsors.sql', 'test-db.sql', 'judge-bridge.py', 'magpie_judge.py',
))
os.umask(0o077)
STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
install_lock = lock_installation(STATE)
baseline={}
production=Path.home()/'github/firecrawl'
for name in ['.env','docker-compose.yaml','docker-compose.override.yaml']:
    p=production/name
    if p.is_file():
        baseline[name]=hashlib.sha256(p.read_bytes()).hexdigest()
core_containers = output(['docker','ps','--filter','label=com.docker.compose.project=firecrawl',
                          '--format','{{.Names}}']).splitlines()
baseline['containers']={name:json.loads(output(['docker','inspect',name]))[0]['State']['StartedAt']
                        for name in core_containers}
atomic_private(STATE/'production-before.json',json.dumps(baseline,indent=2))

if not ENVFILE.exists():
    if output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+PROJECT]):
        raise RuntimeError('Unowned existing monitor project; stop before touching it')
    config = install_images(release, {})
    config['APP_DB_PASSWORD'] = secrets.token_hex(32)
else:
    saved = dict(line.split('=',1) for line in ENVFILE.read_text().splitlines() if '=' in line)
    config = install_images(release, saved)
    if release:
        verify_project_images(release, PROJECT)
config['JUDGE_SOCKET_DIR'] = str(STATE/'judge')
if release is None:
    config['MONITOR_IMAGE'] = 'firecrawl-monitor:' + SOURCE.name
    base_tag = 'firecrawl-monitor-base:' + config['BASE_API'].split(':')[-1]
    run(['docker','tag',config['BASE_API'],base_tag])
    assert output(['docker','image','inspect',base_tag,'--format','{{.Id}}'])==config['BASE_API']
    run(['docker','build','--network=none','--pull=false','--build-arg','BASE_API='+base_tag,
         '-t',config['MONITOR_IMAGE'],'-f',str(HERE/'Dockerfile'),str(SOURCE)])
atomic_private(ENVFILE,'\n'.join(f'{k}={v}' for k,v in config.items())+'\n')
compose_env = {key: value for key, value in os.environ.items() if key not in config}
compose('up','--no-build','--pull','never','-d','app-db','redis','rabbitmq')
for _ in range(30):
    check=subprocess.run(['docker','exec','firecrawl-monitor-app-db-1','pg_isready','-U','postgres','-d','monitor'],capture_output=True)
    if check.returncode==0:break
    time.sleep(1)
else: raise RuntimeError('Isolated PostgreSQL readiness timeout')
# The cached image's init SQL may not have run (root-only source permissions).
# Apply the pinned queue schema explicitly to its own postgres DB before workers.
queue_schema = SOURCE / ('nuq.sql' if release else 'apps/nuq-postgres/nuq.sql')
compose('exec','-T','app-db','psql','-X','-U','postgres','-d','postgres','-v','ON_ERROR_STOP=1',
        input=queue_schema.read_text())
psql((HERE/'001-monitor.sql').read_text())
psql((HERE/'002-artifact-gc.sql').read_text())
psql((HERE/'003-agent-sponsors.sql').read_text())
clients=STATE/'client.json'
if not clients.exists():
    test_sql=(HERE/'test-db.sql').read_text()
    def phase(name):
        command=['docker','compose','--project-name',PROJECT,'--env-file',str(ENVFILE),'-f',str(HERE/'compose.yaml'),
            'exec','-T','app-db','psql','-X','-U','postgres','-d','monitor','-v','ON_ERROR_STOP=1','-v','phase='+name]
        result=run(command,input=test_sql,capture_output=True,env=compose_env)
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
units=Path.home()/'.config/systemd/user'
units.mkdir(parents=True,exist_ok=True,mode=0o700)
unit=units/'firecrawl-monitor-judge.service'
unit.write_text(f'''[Unit]
Description=Firecrawl Monitor Private Magpie Judge
# Magpie is managed by the system manager, outside this user unit's ordering.
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
compose('up','--no-build','--pull','never','-d','api')
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
prune_unit=units/'firecrawl-monitor-prune.service'
prune_unit.write_text('''[Unit]
Description=Firecrawl Monitor Retention Pruning
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec firecrawl-monitor-api-1 node /app/monitor-prune.cjs
TimeoutStartSec=120
NoNewPrivileges=true
''')
(units/'firecrawl-monitor-prune.timer').write_text('''[Unit]
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
receipt = {'status':'ready','monitor_url':'http://127.0.0.1:3006','project':PROJECT,
           'production_unchanged':True,'release':str(SOURCE)}
if release:
    receipt['release_manifest'] = release
    atomic_private(STATE/'deployment.json', json.dumps(receipt, indent=2))
print(json.dumps(receipt))
