import json,time,urllib.request
from pathlib import Path
c=json.loads(Path('/home/marck/services/firecrawl-monitor/shared/client.json').read_text())
base='http://127.0.0.1:3006'
def call(path,method='GET',body=None):
    r=urllib.request.Request(base+path,method=method,data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+c['api_key'],'Content-Type':'application/json'})
    with urllib.request.urlopen(r,timeout=30) as response:return json.load(response)
monitor=None
try:
    value=call('/v2/monitor','POST',{'name':'temporary dependency repair acceptance',
        'schedule':{'cron':'0 0 * * *','timezone':'UTC'},'targets':[{'type':'scrape',
        'urls':['https://httpbingo.org/range/64'],'scrapeOptions':{'formats':['markdown'],
        'headers':{'Range':'bytes=0-15'},'fastMode':True,'maxAge':0,'storeInCache':False,'timeout':20000}}],
        'judgeEnabled':False,'notification':{}})
    monitor=value['data']['id']
    check=call('/v2/monitor/'+monitor+'/run','POST')['id']
    for _ in range(65):
        result=call('/v2/monitor/'+monitor+'/checks/'+check)['data']
        if result['status'] not in ('queued','running'):
            if result['status']!='completed' or result['summary']['error']!=0:raise RuntimeError('monitor check failed')
            print('monitor create and real check passed');break
        time.sleep(2)
    else: raise TimeoutError('monitor check timeout')
finally:
    if monitor:
        call('/v2/monitor/'+monitor,'DELETE')
        print('temporary monitor deleted')
