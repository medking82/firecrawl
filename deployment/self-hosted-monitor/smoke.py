"""Live native monitor lifecycle/LLM test; uses only its own monitor and synthetic public data."""
import json,subprocess,sys,time,urllib.error,urllib.request,uuid
from pathlib import Path

STATE=Path('/home/marck/services/firecrawl-monitor/shared')
CLIENT=json.loads((STATE/'client.json').read_text())
BASE='http://127.0.0.1:3006'
RECEIPTS=[]
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def call(name,method,path,body=None,key=CLIENT['api_key'],expected=200):
    headers={'Content-Type':'application/json'}
    if key:headers['Authorization']='Bearer '+key
    request=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    try:
        with OPENER.open(request,timeout=15) as response: status,raw=response.status,response.read(2_000_001)
    except urllib.error.HTTPError as response: status,raw=response.code,response.read(2_000_001)
    assert len(raw)<=2_000_000,'response limit'
    value=json.loads(raw)
    RECEIPTS.append({'case':name,'method':method,'path':path,'http_status':status,'response':value})
    (STATE/'smoke-results.json').write_text(json.dumps(RECEIPTS,indent=2))
    if expected is not None:assert status==expected,(name,status,value)
    print(json.dumps({'case':name,'http_status':status}),flush=True)
    return value
def sql(query):
    result=subprocess.run(['docker','exec','-i','firecrawl-monitor-app-db-1','psql','-X','-U','postgres','-d','monitor','-v','ON_ERROR_STOP=1','-At'],input=query,text=True,check=True,capture_output=True)
    return result.stdout
def wait_check(monitor,check_id,name):
    for _ in range(65):
        value=call(name,'GET',f'/v2/monitor/{monitor}/checks/{check_id}')['data']
        if value['status'] not in ('queued','running'):
            assert value['status']=='completed',(name,value)
            assert value['summary']['error']==0,(name,value)
            return value
        time.sleep(2)
    raise TimeoutError(name)
def run_check(monitor,name):
    check=call(name+'-create','POST',f'/v2/monitor/{monitor}/run')
    return wait_check(monitor,check['id'],name)

monitor=None
other_team=str(uuid.uuid4()); other_key=str(uuid.uuid4())
try:
    call('readiness','GET','/v0/health/readiness',key=None)
    denied=call('no-auth','GET','/v2/monitor',key=None,expected=None)
    assert not denied.get('success',False),'Missing auth was accepted'
    call('invalid-key','GET','/v2/monitor',key=str(uuid.uuid4()),expected=401)
    call('invalid-input','POST','/v2/monitor',{'name':'invalid'},expected=400)
    body={'name':'GA401 native monitor acceptance','schedule':{'cron':'0 0 * * *','timezone':'UTC'},
          'targets':[{'type':'scrape','urls':['https://httpbingo.org/range/64'],
              'scrapeOptions':{'formats':['markdown'],'headers':{'Range':'bytes=0-15'},'fastMode':True,'maxAge':0,'storeInCache':False,'timeout':20000}}],
          'judgeEnabled':False,'notification':{}}
    created=call('create','POST','/v2/monitor',body)
    monitor=created['data']['id']
    (STATE/'owned-smoke-monitor.json').write_text(json.dumps({'monitor_id':monitor}))
    sql(f"INSERT INTO public.teams(id,name) VALUES ('{other_team}','monitor isolation test'); INSERT INTO public.api_keys(key,team_id) VALUES ('{other_key}','{other_team}');")
    call('other-tenant','GET',f'/v2/monitor/{monitor}',key=other_key,expected=404)
    first=run_check(monitor,'baseline-new')
    assert first['summary']['new']==1,first
    same=run_check(monitor,'repeat-same')
    assert same['summary']['same']==1,same
    target=created['data']['targets'][0]
    target['scrapeOptions']['headers']['Range']='bytes=16-31'
    call('enable-cli-judge','PATCH',f'/v2/monitor/{monitor}',{
        'targets':[target],'goal':'Alert when any of the monitored alphabetic text changes. Exact text changes matter.',
        'judgeEnabled':True})
    changed=run_check(monitor,'changed-with-cli-judge')
    assert changed['summary']['changed']==1,changed
    page=changed['pages'][0]
    assert page['diff'] and page['diff'].get('text'),'Changed diff must be retrievable from local storage'
    judgment=page.get('judgment')
    assert judgment and judgment['meaningful'] is True,judgment
    assert judgment['confidence'] in ['high','medium'],judgment
    assert 'failed' not in judgment['reason'].lower() and judgment['meaningfulChanges'],judgment
    call('pause','PATCH',f'/v2/monitor/{monitor}',{'status':'paused'})
    call('paused-run','POST',f'/v2/monitor/{monitor}/run',expected=409)
    call('resume','PATCH',f'/v2/monitor/{monitor}',{'status':'active','schedule':{'cron':'*/5 * * * *','timezone':'UTC'}})
    # Only the owned test row is retimed; the genuine 60-second scheduler and
    # SQL lease claim must enqueue and complete this scheduled occurrence.
    sql(f"UPDATE public.monitors SET next_run_at=now()-interval '10 minutes',locked_until=NULL WHERE id='{monitor}';")
    scheduled=None
    for _ in range(75):
        checks=call('scheduled-poll','GET',f'/v2/monitor/{monitor}/checks')['data']
        for check in checks:
            if check['trigger']=='scheduled':scheduled=check;break
        if scheduled:break
        time.sleep(2)
    assert scheduled,'Native scheduler did not produce a check'
    completed=wait_check(monitor,scheduled['id'],'scheduled-completed')
    assert completed['summary']['same']==1,completed
    print(json.dumps({'status':'PASS','monitor_id':monitor,'scheduled_check':scheduled['id'],'cli_judgment':judgment,'baseline_retained':True}),flush=True)
finally:
    if monitor:
        call('delete-owned-monitor','DELETE',f'/v2/monitor/{monitor}')
        call('deleted-not-found','GET',f'/v2/monitor/{monitor}',expected=404)
    sql(f"DELETE FROM public.api_keys WHERE key='{other_key}'; DELETE FROM public.teams WHERE id='{other_team}';")
