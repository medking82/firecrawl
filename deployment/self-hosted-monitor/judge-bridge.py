"""Private Unix-socket bridge to an existing tools-disabled CLI subscription route."""
import http.server,json,os,socketserver,threading,urllib.request
from pathlib import Path

SOCKET=Path(os.environ['MONITOR_JUDGE_SOCKET'])
MODEL=os.environ.get('MONITOR_JUDGE_MODEL','claude-cli/claude-sonnet-5-5')
if MODEL not in {'claude-cli/claude-sonnet-5-5','claude-cli/claude-opus-5-5'}:
    raise ValueError('Only verified tools-disabled Claude CLI routes are eligible')
TOKEN_FILE=Path('/home/marck/.opencodex/service-api-token')
BUSY=threading.BoundedSemaphore(1)
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass # Never log page content, goals or credentials.
    def reply(self,code,value):
        data=json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        if self.path!='/judge': return self.reply(404,{'error':'not_found'})
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=128000: return self.reply(413,{'error':'input_limit'})
        if not BUSY.acquire(blocking=False): return self.reply(429,{'error':'judge_busy'})
        try:
            self.connection.settimeout(125)
            value=json.loads(self.rfile.read(size))
            if set(value)!={'system','prompt'} or any(not isinstance(value[k],str) for k in value):
                return self.reply(400,{'error':'invalid_input'})
            payload={'model':MODEL,'messages':[{'role':'system','content':value['system']},{'role':'user','content':value['prompt']}],
                     'stream':False,'max_tokens':2500,'tools':[]}
            headers={'Content-Type':'application/json'}
            if TOKEN_FILE.is_file(): headers['Authorization']='Bearer '+TOKEN_FILE.read_text().strip()
            req=urllib.request.Request('http://127.0.0.1:10100/v1/chat/completions',data=json.dumps(payload).encode(),headers=headers)
            with OPENER.open(req,timeout=110) as response:
                raw=response.read(256001)
            if len(raw)>256000: raise ValueError('response_limit')
            body=json.loads(raw)
            text=body['choices'][0]['message']['content']
            if not isinstance(text,str) or not text.strip(): raise ValueError('empty_judgment')
            # Report provider/model only. HTTP 200 is not sufficient to prove valid judgment JSON.
            print(json.dumps({'event':'judge_completed','model':MODEL,'output_chars':len(text)}),flush=True)
            self.reply(200,{'text':text,'model':MODEL})
        except Exception as exc:
            print(json.dumps({'event':'judge_failed','error_type':type(exc).__name__}),flush=True)
            self.reply(502,{'error':'local_cli_judge_failed'})
        finally: BUSY.release()

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True

SOCKET.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
if SOCKET.exists(): SOCKET.unlink() # Owned socket, service is single-instance.
with Server(str(SOCKET),Handler) as server:
    os.chmod(SOCKET,0o600)
    server.serve_forever()
