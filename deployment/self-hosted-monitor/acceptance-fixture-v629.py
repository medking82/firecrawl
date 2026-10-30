"""Disposable 629 monitor API, queue, and database acceptance; never uses production volumes."""
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = 'sha256:89b2bacf0c5f6d9a183d8f9f5ff9ff5ae64a1757b61fd5509990aa2f804398f4'
DB = 'sha256:1b3f0c48add7f26ed3a9df806d324c8048226ee75a486f451b08b527d8be8a08'
REDIS = 'sha256:978f0e01593e65eed801f2402944efcd936d43b5027e4908a7897baf88ed6241'
RABBIT = 'sha256:e582c0bc7766f3342496d8485efb5a1df782b5ce3886ad017e2eaae442311f69'


def run(args, *, input=None, timeout=60):
    return subprocess.run(args, input=input, capture_output=True, text=True, check=True, timeout=timeout).stdout.strip()


def main():
    token = uuid.uuid4().hex; network = 'monitor-v629-fixture-' + token
    containers = []; monitor = None; db = None; rabbit_volume = network + '-rabbit'
    volume_created = False
    team, key = str(uuid.uuid4()), str(uuid.uuid4())
    def start(image, name, args):
        result = run(['docker', 'run', '-d', '--network', network, '--network-alias', name] + args + [image])
        containers.append(result); return result
    def sql(database, text):
        return run(['docker', 'exec', '-i', db, 'psql', '-X', '-U', 'postgres', '-d', database,
                    '-v', 'ON_ERROR_STOP=1'], input=text)
    try:
        run(['docker', 'network', 'create', network])
        run(['docker', 'volume', 'create', rabbit_volume]); volume_created = True
        db = start(DB, 'app-db', ['--tmpfs', '/var/lib/postgresql/data:rw,size=512m', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust'])
        redis = start(REDIS, 'redis', ['--tmpfs', '/data:rw,size=64m'])
        rabbit = start(RABBIT, 'rabbitmq', ['--mount', 'type=volume,src='+rabbit_volume+',dst=/var/lib/rabbitmq', '-e', 'RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2 +A 4'])
        for _ in range(60):
            ready = subprocess.run(['docker','exec',db,'pg_isready','-U','postgres','-d','postgres'], capture_output=True).returncode == 0
            mq = subprocess.run(['docker','exec','--user','999:999',rabbit,'rabbitmq-diagnostics','-q','check_running'], capture_output=True).returncode == 0
            if ready and mq: break
            time.sleep(1)
        else: raise RuntimeError('fixture dependencies not ready')
        run(['docker','exec',db,'createdb','-U','postgres','monitor'])
        for name in ('001-monitor.sql','002-artifact-gc.sql','003-agent-sponsors.sql'):
            sql('monitor', (HERE/name).read_text())
        sql('monitor', "INSERT INTO teams(id,name) VALUES ('%s','isolated fixture'); INSERT INTO api_keys(key,team_id,name) VALUES ('%s','%s','fixture'); INSERT INTO blocklist(data) VALUES ('{\"blocklist\":[],\"allowedKeywords\":[]}');" % (team,key,team))
        env = {'HOST':'0.0.0.0','PORT':'3002','ENV':'local','USE_DB_AUTHENTICATION':'true',
               'DATABASE_URL':'postgresql://postgres@fixture-app-db:5432/monitor',
               'DISABLE_MONITORING':'false','SELF_HOSTED_ARTIFACT_DIR':'/tmp/artifacts',
               'REDIS_URL':'redis://fixture-redis:6379','REDIS_RATE_LIMIT_URL':'redis://fixture-redis:6379',
               'NUQ_BACKEND':'pg','NUQ_RABBITMQ_URL':'amqp://fixture-rabbitmq:5672',
               'POSTGRES_HOST':'fixture-app-db','POSTGRES_USER':'postgres','POSTGRES_PASSWORD':'fixture',
               'POSTGRES_DB':'postgres','POSTGRES_PORT':'5432','NUM_WORKERS_PER_QUEUE':'1','NUQ_WORKER_COUNT':'1',
               'CRAWL_CONCURRENT_REQUESTS':'1','MAX_CONCURRENT_JOBS':'1','BROWSER_POOL_SIZE':'1',
               'HARNESS_STARTUP_TIMEOUT_MS':'120000','LOGGING_LEVEL':'info'}
        # Name aliases are unique within this fixture's private network.
        env['DATABASE_URL'] = 'postgresql://postgres@app-db:5432/monitor'
        env['REDIS_URL'] = env['REDIS_RATE_LIMIT_URL'] = 'redis://redis:6379'
        env['NUQ_RABBITMQ_URL'] = 'amqp://rabbitmq:5672'; env['POSTGRES_HOST'] = 'app-db'
        args=['-p','127.0.0.1::3002','--tmpfs','/tmp:rw,size=256m,mode=1777']
        for k,v in env.items(): args += ['-e', k+'='+v]
        api=start(IMAGE,'api',args)
        port=run(['docker','port',api,'3002/tcp']).rsplit(':',1)[1]
        base='http://127.0.0.1:'+port
        def call(path,method='GET',body=None):
            req=urllib.request.Request(base+path,method=method,data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=15) as response: return json.load(response)
        for _ in range(100):
            try:
                if call('/v0/health/readiness').get('status')=='ok': break
            except Exception: pass
            time.sleep(2)
        else: raise RuntimeError('fixture API readiness timeout')
        created=call('/v2/monitor','POST',{'name':'isolated 629 acceptance','schedule':{'cron':'0 0 * * *','timezone':'UTC'},
            'targets':[{'type':'scrape','urls':['https://httpbingo.org/range/64'],'scrapeOptions':{'formats':['markdown'],
            'headers':{'Range':'bytes=0-15'},'fastMode':True,'maxAge':0,'storeInCache':False,'timeout':20000}}],
            'judgeEnabled':False,'notification':{}})
        monitor=created['data']['id']; check=call('/v2/monitor/'+monitor+'/run','POST')['id']
        for _ in range(65):
            result=call('/v2/monitor/'+monitor+'/checks/'+check)['data']
            if result['status'] not in ('queued','running'):
                if result['status']!='completed' or result['summary']['error']!=0: raise RuntimeError('fixture monitor check failed')
                break
            time.sleep(2)
        else: raise RuntimeError('fixture monitor timeout')
        call('/v2/monitor/'+monitor,'DELETE'); monitor=None
        print(json.dumps({'status':'passed','image':IMAGE,'scope':'isolated monitor create/run/delete','judge':False}))
    finally:
        for item in reversed(containers): subprocess.run(['docker','rm','-f',item],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if volume_created:
            subprocess.run(['docker','volume','rm',rabbit_volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','network','rm',network],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__ == '__main__': main()
