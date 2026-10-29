"""Offline, pinned browser overlay. Existing core/monitor Compose projects are read-only."""
import hashlib,json,os,secrets,subprocess,time,uuid,urllib.request
from pathlib import Path

HERE=Path(__file__).resolve().parent
SOURCE=HERE.parents[1]
ROOT=Path('/home/marck/services/firecrawl-browser')
STATE=ROOT/'shared'
ENVFILE=STATE/'runtime.env'
PROJECT='firecrawl-browser'
FILES=['lib/local-artifacts','lib/gcs-jobs','lib/gcs-monitoring','lib/local-monitor-judge',
       'services/monitoring/judgeChange','services/logging/log_job','lib/local-hangar','lib/hangar',
       'lib/local-browser-agent','lib/scrape-interact/browser-agent','controllers/v2/browser',
       'lib/local-browser-job-store','lib/job-access-store','lib/job-state-store']

def run(args,**kwargs):return subprocess.run(args,check=True,text=True,**kwargs)
def output(args):return subprocess.check_output(args,text=True).strip()
def private(path,text):
    temp=path.with_suffix('.tmp');temp.write_text(text);temp.chmod(0o600);temp.replace(path)
def snapshot():
    names=output(['docker','ps','--format','{{.Names}}']).splitlines()
    containers={}
    for name in names:
        if name.startswith('firecrawl-') and not name.startswith('firecrawl-browser-'):
            row=json.loads(output(['docker','inspect',name]))[0]
            containers[name]={'image':row['Image'],'started':row['State']['StartedAt']}
    base=Path('/home/marck/github/firecrawl')
    return {'containers':containers,'production':{n:hashlib.sha256((base/n).read_bytes()).hexdigest() for n in ['.env','docker-compose.yaml','docker-compose.override.yaml']}}
def compose(*args,**kwargs):return run(['docker','compose','--project-name',PROJECT,'--env-file',str(ENVFILE),'-f',str(HERE/'compose.yaml'),*args],**kwargs)
def psql(sql,db='browser'):
    return compose('exec','-T','app-db','psql','-X','-U','postgres','-d',db,'-v','ON_ERROR_STOP=1',input=sql)

os.umask(0o077)
STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
before=snapshot();private(STATE/'before.json',json.dumps(before,indent=2))
if not ENVFILE.exists():
    if output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+PROJECT]):raise RuntimeError('unowned_browser_project')
    config={k:json.loads(output(['docker','inspect',v]))[0]['Image'] for k,v in {
        'BASE_API':'firecrawl-api-1','POSTGRES_IMAGE':'firecrawl-nuq-postgres-1','REDIS_IMAGE':'firecrawl-redis-1',
        'RABBIT_IMAGE':'firecrawl-rabbitmq-1','BROWSER_IMAGE':'firecrawl-playwright-service-1'}.items()}
    config['APP_DB_PASSWORD']=secrets.token_hex(32)
else:config=dict(line.split('=',1) for line in ENVFILE.read_text().splitlines() if '=' in line)
config.update(BROWSER_API_IMAGE='firecrawl-browser:'+SOURCE.name,BROWSER_STATE_DIR=str(STATE))
private(ENVFILE,'\n'.join(k+'='+v for k,v in config.items())+'\n')
tag='firecrawl-browser-base:'+config['BASE_API'].split(':')[1]
run(['docker','tag',config['BASE_API'],tag])
assert output(['docker','image','inspect',tag,'--format','{{.Id}}'])==config['BASE_API']
manifest={}
for item in FILES:
    p=SOURCE/'apps/api/src'/(item+'.ts');manifest[str(p.relative_to(SOURCE))]=hashlib.sha256(p.read_bytes()).hexdigest()
private(HERE/'files.json',json.dumps(FILES))
dockerfile=['ARG BASE_API','FROM ${BASE_API}','USER root']
dockerfile += [f'COPY apps/api/src/{p}.ts /tmp/browser-src/{p}.ts' for p in FILES]
dockerfile += ['COPY deployment/self-hosted-browser/files.json /tmp/browser-files.json',
              'COPY deployment/self-hosted-browser/compile.cjs /tmp/compile-browser.cjs','RUN node /tmp/compile-browser.cjs']
private(HERE/'Dockerfile.generated','\n'.join(dockerfile)+'\n')
run(['docker','build','--network=none','--pull=false','--build-arg','BASE_API='+tag,'-t',config['BROWSER_API_IMAGE'],'-f',str(HERE/'Dockerfile.generated'),str(SOURCE)])
compose('up','-d','app-db','redis','rabbitmq')
for _ in range(40):
    if subprocess.run(['docker','exec','firecrawl-browser-app-db-1','pg_isready','-U','postgres','-d','browser'],capture_output=True).returncode==0:break
    time.sleep(1)
else:raise RuntimeError('browser_db_not_ready')
psql((SOURCE/'apps/nuq-postgres/nuq.sql').read_text(),'postgres')
psql((SOURCE/'deployment/self-hosted-monitor/001-monitor.sql').read_text())
psql((HERE/'001-browser.sql').read_text())
psql((HERE/'002-job-store.sql').read_text())
clients=STATE/'client.json'
if not clients.exists():private(clients,json.dumps({'api_url':'http://127.0.0.1:13007','remote_url':'http://127.0.0.1:3007','team_id':str(uuid.uuid4()),'api_key':str(uuid.uuid4())}))
client=json.loads(clients.read_text());team=str(uuid.UUID(client['team_id']));key=str(uuid.UUID(client['api_key']))
psql(f"INSERT INTO public.teams(id,name,org_id) VALUES ('{team}','GA401 self-hosted browser',NULL) ON CONFLICT(id) DO NOTHING;\n"
     f"INSERT INTO public.api_keys(key,team_id,name) VALUES ('{key}','{team}','local browser') ON CONFLICT(key) DO NOTHING;\n"
     "INSERT INTO public.blocklist(data,org_id) SELECT '{\"blocklist\":[],\"allowedKeywords\":[]}'::jsonb,NULL WHERE NOT EXISTS(SELECT 1 FROM public.blocklist WHERE org_id IS NULL);\n"
     f"INSERT INTO public.self_hosted_team_policy(team_id,flags) VALUES ('{team}','{{\"rateLimitOverrides\":{{\"crawl\":30,\"crawlStatus\":120,\"browser\":30,\"browserExecute\":120,\"browserReplay\":60}}}}'::jsonb) ON CONFLICT(team_id) DO UPDATE SET flags=jsonb_set(self_hosted_team_policy.flags,'{{rateLimitOverrides}}',coalesce(self_hosted_team_policy.flags->'rateLimitOverrides','{{}}'::jsonb)||(excluded.flags->'rateLimitOverrides'));\n")
# Invalidate only this deployment's credential/team policy cache, never queues.
run(['docker','exec','firecrawl-browser-redis-1','redis-cli','DEL',
     f'acuc_general_{key}_scrape',f'acuc_general_{key}_extract',
     f'acuc_team_{team}_scrape',f'acuc_team_{team}_extract'],capture_output=True)
units=Path('/home/marck/.config/systemd/user');units.mkdir(parents=True,exist_ok=True)
for name,exe,env in [
    ('manager',f'/usr/bin/node {HERE}/manager.cjs',{'BROWSER_STATE_DIR':str(STATE),'BROWSER_IMAGE':config['BROWSER_IMAGE'],'BROWSER_PUBLIC_WS':'ws://127.0.0.1:13008'}),
    ('llm',f'/usr/bin/python3 -B {HERE}/browser-bridge.py',{'SELF_HOSTED_BROWSER_LLM_SOCKET':str(STATE/'llm/browser.sock')})]:
    (units/f'firecrawl-browser-{name}.service').write_text('[Unit]\nDescription=Firecrawl Self-hosted Browser '+name+'\n[Service]\nType=simple\nExecStart='+exe+'\n'+''.join('Environment='+k+'='+v+'\n' for k,v in env.items())+'UMask=0077\nRestart=on-failure\nRestartSec=10\nNoNewPrivileges=true\n[Install]\nWantedBy=default.target\n')
run(['systemctl','--user','daemon-reload'])
run(['systemctl','--user','enable','--now','firecrawl-browser-manager.service','firecrawl-browser-llm.service'])
run(['systemctl','--user','restart','firecrawl-browser-manager.service','firecrawl-browser-llm.service'])
for _ in range(30):
    if (STATE/'manager.sock').exists() and (STATE/'llm/browser.sock').exists():break
    time.sleep(1)
else:raise RuntimeError('browser_bridges_not_ready')
compose('up','-d','api')
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:3007/v0/health/readiness',timeout=2) as r:
            if json.load(r).get('status')=='ok':break
    except Exception:pass
    time.sleep(1)
else:
    compose('stop','api');raise RuntimeError('browser_api_not_ready')
after=snapshot();assert before==after,'existing_service_changed'
receipt={'release':SOURCE.name,'sources':manifest,'before':before,'after':after,'core_and_monitor_unchanged':True,'api_port':3007,'cdp_port':3008,'public_api_port':13007,'public_cdp_port':13008}
private(STATE/'deployment.json',json.dumps(receipt,indent=2));print(json.dumps(receipt),flush=True)
