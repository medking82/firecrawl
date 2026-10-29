"""Real native API lifecycle, isolation, ownership and Codex CLI interaction regression."""
import json,subprocess,time,urllib.request,urllib.error,uuid
from pathlib import Path

STATE=Path('/home/marck/services/firecrawl-browser/shared')
c=json.loads((STATE/'client.json').read_text())
owned=[];checks={};second_team=str(uuid.uuid4());second_key=str(uuid.uuid4())

def sql(text):
    return subprocess.check_output(['docker','exec','-i','firecrawl-browser-app-db-1','psql','-X','-U','postgres','-d','browser','-v','ON_ERROR_STOP=1','-At'],input=text,text=True).strip()
def call(path,body=None,method=None,key=None,expected=200):
    headers={'Authorization':'Bearer '+(key or c['api_key']),'Content-Type':'application/json'}
    req=urllib.request.Request(c['remote_url']+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=150) as r:status=r.status;value=json.load(r)
    except urllib.error.HTTPError as exc:status=exc.code;value=json.load(exc)
    assert status==expected,(path,status,value)
    return value
def create(**opts):
    value=call('/v2/browser',{'ttl':180,'activityTtl':120,**opts});owned.append(value['id']);return value
def execute(id,code,timeout=30):
    return call('/v2/browser/'+id+'/execute',{'code':code,'language':'node','timeout':timeout})
def passed(name,value=True):
    checks[name]=value;print('PASS',name,flush=True)

try:
    sql(f"INSERT INTO teams(id,name,org_id) VALUES ('{second_team}','browser smoke other tenant',NULL); INSERT INTO api_keys(key,team_id,name) VALUES ('{second_key}','{second_team}','smoke');")
    call('/v2/browser',key=str(uuid.uuid4()),expected=401);passed('authentication')
    value=create();id=value['id']
    assert value['cdpUrl'].startswith('ws://127.0.0.1:13008/cdp/')
    assert call('/v2/browser/'+id)['status']=='running'
    assert id in [s['id'] for s in call('/v2/browser')['sessions']]
    passed('create_status_list')
    call('/v2/browser/'+id,key=second_key,expected=403)
    call('/v2/browser/'+id+'/execute',{'code':'console.log(1)','language':'node','timeout':10},key=second_key,expected=403)
    call('/v2/browser/'+id,method='DELETE',key=second_key,expected=403);passed('tenant_ownership')
    out=execute(id,"await page.goto('https://example.com'); console.log(await page.title());")
    assert out['exitCode']==0 and 'Example Domain' in out['stdout'];passed('public_navigation')
    out=execute(id,"await page.setContent('<title>Persistent Smoke</title><p id=proof>first</p>');")
    assert out['exitCode']==0
    out=execute(id,"console.log(await page.title(),await page.locator('#proof').innerText());")
    assert out['exitCode']==0 and 'Persistent Smoke first' in out['stdout'];passed('persistent_page')
    out=execute(id,"throw new Error('owned-smoke-failure')")
    assert out['exitCode']!=0 and 'owned-smoke-failure' in out['stderr'];passed('execution_failure')
    out=execute(id,'await new Promise(()=>{})',timeout=1)
    assert out['killed'] and out['exitCode']!=0
    assert execute(id,'console.log(await page.title())')['exitCode']==0;passed('timeout_then_recovery')
    out=execute(id,"console.log(JSON.stringify({auth:require('fs').existsSync('/home/marck/.codex/auth.json'),docker:require('fs').existsSync('/var/run/docker.sock')})); try { await fetch('http://127.0.0.1:3002',{signal:AbortSignal.timeout(1000)});console.log('HOST_REACHABLE'); } catch { console.log('HOST_BLOCKED'); }")
    assert out['exitCode']==0 and '"auth":false' in out['stdout'] and '"docker":false' in out['stdout'] and 'HOST_BLOCKED' in out['stdout'];passed('container_isolation')
    out=execute(id,"const http=require('node:http'),net=require('node:net'); await new Promise((resolve,reject)=>{const r=http.get({host:'169.254.169.254',path:'http://169.254.169.254/latest/meta-data/',createConnection:()=>net.connect('/run/egress/proxy.sock')},res=>{console.log(res.statusCode);res.resume();res.on('end',resolve)});r.on('error',reject);});")
    assert out['exitCode']==0 and '403' in out['stdout'];passed('metadata_egress_blocked')
    call('/v2/browser',{'streamWebView':True},expected=422)
    call('/v2/browser',{'profile':{'name':'smoke'}},expected=422);passed('unsupported_options_fail_closed')
    expiring=create(ttl=30,activityTtl=10)['id']
    time.sleep(12)
    assert call('/v2/browser/'+expiring)['status']=='stopped'
    assert call('/v2/browser/'+expiring+'/execute',{'code':'console.log(1)','language':'node','timeout':5},expected=410)['success']==False
    passed('idle_cleanup')
    stopped=call('/v2/browser/'+id,method='DELETE');assert stopped['status']=='stopped'
    assert call('/v2/browser/'+id,method='DELETE')['creditsBilled']==stopped['creditsBilled'];passed('idempotent_stop_settlement')
    alias=call('/v2/interact',{'ttl':60,'activityTtl':30});owned.append(alias['id'])
    out=call('/v2/interact/'+alias['id']+'/execute',{'code':"console.log('ALIAS_OK')",'language':'node','timeout':10})
    assert out['exitCode']==0 and 'ALIAS_OK' in out['stdout'];call('/v2/interact/'+alias['id'],method='DELETE');passed('interact_alias')
    scrape=call('/v2/scrape',{'url':'https://example.com','formats':['markdown'],'blockAds':False})
    scrape_id=scrape['data']['metadata']['scrapeId']
    linked='/v2/scrape/'+scrape_id+'/interact'
    call(linked,{'code':'console.log(1)','language':'node','timeout':10},key=second_key,expected=403);passed('scrape_ownership')
    interaction=call(linked,{'code':"console.log(await page.title());",'language':'node','timeout':30})
    assert interaction['success'] and 'Example Domain' in interaction['stdout']
    owned.append(interaction['sessionId']);passed('scrape_replay')
    marker='BROWSER_LLM_OK_'+uuid.uuid4().hex[:10]
    code="await page.setContent("+json.dumps('<title>Browser CLI Smoke</title><button id="finish" onclick="document.querySelector(\'#proof\').textContent=\''+marker+'\'">Finish Smoke</button><p id="proof">PENDING</p>')+");"
    assert call(linked,{'code':code,'language':'node','timeout':20})['success']
    result=call(linked,{'prompt':'Click the Finish Smoke button and report the exact proof text that appears afterward.','timeout':120})
    assert result['success'],result
    assert marker in result.get('output','') or marker in result.get('stdout',''),result
    actual=call(linked,{'code':"console.log(await page.locator('#proof').innerText())",'language':'node','timeout':10})
    assert marker in actual['stdout'];passed('native_codex_cli_action_and_verification',{'marker':marker,'session_id':interaction['sessionId']})
    assert call(linked,method='DELETE')['status']=='stopped';passed('interact_cleanup')
    # TTL is independent of activity. Start a new session and keep it active beyond ttl.
    hard=create(ttl=30,activityTtl=60)['id']
    for _ in range(4):
        time.sleep(8)
        if call('/v2/browser/'+hard)['status']=='stopped':break
        assert execute(hard,"console.log('keepalive')",timeout=5)['exitCode']==0
    assert call('/v2/browser/'+hard)['status']=='stopped';passed('hard_ttl_cleanup')
finally:
    errors=[]
    for id in owned:
        try:call('/v2/browser/'+id,method='DELETE')
        except Exception as exc:errors.append(type(exc).__name__)
    sql(f"DELETE FROM api_keys WHERE team_id='{second_team}';DELETE FROM self_hosted_team_policy WHERE team_id='{second_team}';DELETE FROM teams WHERE id='{second_team}';")
    remaining=subprocess.check_output(['docker','ps','-aq','--filter','label=firecrawl.self-hosted-browser=session'],text=True).strip()
    checks['cleanup']={'errors':errors,'remaining_session_containers':remaining.splitlines() if remaining else []}
    (STATE/'smoke-results.json').write_text(json.dumps(checks,indent=2))
    assert not errors and not remaining,checks['cleanup']
print(json.dumps(checks),flush=True)
