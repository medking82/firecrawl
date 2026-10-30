"""Disposable Docker DB fixture; input JSON supplies reviewed schema text and image IDs."""
import json, os, subprocess, sys, time, uuid

cfg = json.loads(sys.stdin.read())
name = 'firecrawl-v626-fixture-' + uuid.uuid4().hex[:12]
image = cfg['postgres_image']
if not image.startswith('sha256:') or len(image) != 71:
    raise ValueError('immutable postgres image required')

def run(args, **kwargs):
    return subprocess.run(['docker', *args], check=True, text=True, **kwargs)

created = False
try:
    run(['run', '-d', '--name', name, '--label', 'firecrawl.fixture=v626', '--pull=never',
         '--network=none', '--tmpfs', '/var/lib/postgresql/data:rw,size=512m',
         '-e', 'POSTGRES_PASSWORD=fixture-only', '-e', 'POSTGRES_DB=postgres', image],
        stdout=subprocess.DEVNULL)
    created = True
    for _ in range(60):
        ready = subprocess.run(['docker','exec',name,'pg_isready','-U','postgres','-d','postgres'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode == 0: break
        status = run(['inspect','--format','{{.State.Running}}',name],capture_output=True).stdout.strip()
        if status != 'true':
            logs = run(['logs','--tail','15',name],capture_output=True)
            raise RuntimeError('disposable postgres exited: '+logs.stdout+logs.stderr)
        time.sleep(1)
    else: raise RuntimeError('disposable postgres readiness timeout')
    def sql(db, text):
        return run(['exec','-i',name,'psql','-X','-U','postgres','-d',db,
                    '-v','ON_ERROR_STOP=1','-At'], input=text, capture_output=True).stdout.strip()
    sql('postgres', 'CREATE DATABASE browser;')
    sql('postgres', cfg['nuq'])
    for key in ['monitor', 'browser', 'job']:
        sql('browser', cfg[key])
    sql('browser', cfg['browser'])
    sql('browser', cfg['job'])
    print('PASS schemas_and_repeat_apply', flush=True)
    out = sql('browser', """
      INSERT INTO public.self_hosted_browser_jobs(kind,id,value,expires_at)
      VALUES ('access','019a3000-0000-7000-8000-000000000001','{"team":"fixture"}',now()+interval '1 hour'),
             ('scrape_state','019a3000-0000-7000-8000-000000000001','{"replay":"fixture"}',now()+interval '1 hour');
      SELECT count(*) FROM public.self_hosted_browser_jobs WHERE value->>'team'='fixture' OR value->>'replay'='fixture';
      SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('browser_sessions','browser_session_activities','browser_profiles','self_hosted_browser_jobs');
    """)
    if out.splitlines()[-2:] != ['2','4']: raise RuntimeError('schema contract mismatch: '+out)
    print('PASS job_store_and_browser_tables', flush=True)
finally:
    if created:
        run(['rm','-f',name], stdout=subprocess.DEVNULL)
        print('PASS fixture_container_removed', flush=True)
