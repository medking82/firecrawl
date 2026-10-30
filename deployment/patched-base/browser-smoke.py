import json, urllib.request
from pathlib import Path
c=json.loads(Path('/home/marck/services/firecrawl-browser/shared/client.json').read_text())
url=c['remote_url']
if url!='http://127.0.0.1:3007': raise SystemExit('unexpected endpoint')
def request(path, method='GET', body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(url+path,data=data,method=method,headers={'Authorization':'Bearer '+c['api_key'],'Content-Type':'application/json'})
    with urllib.request.urlopen(r,timeout=90) as response: return json.load(response)
session=None
try:
    session=request('/v2/browser','POST',{'ttl':180,'activityTtl':120})['id']
    result=request('/v2/browser/'+session+'/execute','POST',{'code':"await page.setContent('<title>recovery</title>'); console.log(await page.title())",'language':'node','timeout':20})
    if 'recovery' not in json.dumps(result): raise RuntimeError('unexpected browser output')
    print('browser create and execute passed')
finally:
    if session:
        result=request('/v2/browser/'+session,'DELETE')
        if result.get('status')!='stopped': raise RuntimeError('browser cleanup failed')
        print('browser session stopped')
