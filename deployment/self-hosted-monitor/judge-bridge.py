"""Private Unix-socket bridge to the actual, text-only native Codex CLI."""
import http.server,json,os,socketserver,threading
from pathlib import Path
from codex_judge import judge

BUSY=threading.BoundedSemaphore(1)

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
        try: size=int(self.headers.get('Content-Length','0'))
        except ValueError: return self.reply(400,{'error':'invalid_length'})
        if not 0<size<=128000: return self.reply(413,{'error':'input_limit'})
        if not BUSY.acquire(blocking=False): return self.reply(429,{'error':'judge_busy'})
        try:
            self.connection.settimeout(125)
            value=json.loads(self.rfile.read(size))
            if set(value)!={'system','prompt'} or any(not isinstance(value[k],str) for k in value):
                return self.reply(400,{'error':'invalid_input'})
            result=judge(value['system'],value['prompt'],model=os.environ.get('MONITOR_JUDGE_MODEL'))
            print(json.dumps({'event':'judge_completed','model':result['model'],
                              'backend':result['backend'],'tool_calls':result['tool_calls'],
                              'settings':result['settings'],'output_chars':len(result['text'])}),flush=True)
            self.reply(200,{'text':result['text'],'model':result['model']})
        except Exception as exc:
            print(json.dumps({'event':'judge_failed','error_type':type(exc).__name__}),flush=True)
            self.reply(502,{'error':'local_cli_judge_failed'})
        finally: BUSY.release()

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True

if __name__=='__main__':
    SOCKET=Path(os.environ['MONITOR_JUDGE_SOCKET'])
    SOCKET.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if SOCKET.exists(): SOCKET.unlink() # Owned socket, service is single-instance.
    with Server(str(SOCKET),Handler) as server:
        os.chmod(SOCKET,0o600)
        server.serve_forever()
