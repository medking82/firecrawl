"""OpenAI-compatible relay from the core Firecrawl API container to GA401 opencodex-proxy.

The core image calls its model provider over HTTP only (OPENAI_BASE_URL), so unlike
the monitor judge and browser LLM bridges this cannot be a Unix socket. It listens
on the docker0 address and admits a request only when all of these hold: the peer
is the current core API container, the bearer is the bridge key from the core .env,
the route is Responses or Chat Completions, the body is JSON and the model is
allowed. The opencodex token stays on the host. Logs never hold prompts, page
content or credentials.
"""
import argparse
import hmac
import http.client
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

LISTEN = ('172.17.0.1', 10180)
UPSTREAM = ('127.0.0.1', 10100)
CONTAINER = 'firecrawl-api-1'
STATE = Path.home() / '.local/state/firecrawl-core-llm'
KEY_FILE = STATE / 'bridge-key'
TOKEN_FILE = Path.home() / '.opencodex/service-api-token'
MODELS = ('gpt-6-luna--fast',)
PATHS = ('/v1/responses', '/v1/chat/completions')
MAX_BODY = 16 * 1024 * 1024
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'server', 'date'}


def container_addresses(name=CONTAINER):
    out = subprocess.run(['docker', 'inspect', '-f',
                          '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}', name],
                         capture_output=True, text=True, timeout=10)
    return set(out.stdout.split()) if out.returncode == 0 else set()


class Peers:
    """Core API container addresses, refreshed when stale or on an unknown peer."""
    def __init__(self, lookup=container_addresses, ttl=30):
        self.lookup, self.ttl, self.lock = lookup, ttl, threading.Lock()
        self.addresses, self.loaded = set(), 0.0

    def allowed(self, address):
        with self.lock:
            fresh = time.monotonic() - self.loaded < self.ttl
            if fresh and address in self.addresses:
                return True
            if fresh and time.monotonic() - self.loaded < 2:
                return False  # Bound docker lookups when an unknown peer retries.
            self.addresses, self.loaded = self.lookup(), time.monotonic()
            return address in self.addresses


def read_secret(path):
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return None
    return value or None


def ensure_key(path=KEY_FILE):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write('fc-llm-' + secrets.token_hex(32) + '\n')
    return read_secret(path)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # Never log paths with content, prompts or credentials.

    def event(self, **values):
        print(json.dumps({'event': 'llm_relay', **values}), flush=True)

    def reply(self, status, code):
        raw = json.dumps({'error': {'message': code, 'type': 'firecrawl_core_llm_bridge'}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True
        self.event(status=status, error=code)

    def do_POST(self):
        self.connection.settimeout(60)
        if not self.server.peers.allowed(self.client_address[0]):
            return self.reply(403, 'peer_not_allowed')
        expected = read_secret(self.server.key_file)
        supplied = self.headers.get('Authorization', '')
        if not expected or not hmac.compare_digest(supplied.encode(), ('Bearer ' + expected).encode()):
            return self.reply(401, 'invalid_bridge_key')
        if self.path not in PATHS:
            return self.reply(404, 'route_not_allowed')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self.reply(415, 'json_required')
        if self.headers.get('Transfer-Encoding'):
            return self.reply(411, 'content_length_required')
        try:
            size = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return self.reply(411, 'content_length_required')
        if not 0 < size <= MAX_BODY:
            return self.reply(413, 'body_limit')
        raw = self.rfile.read(size)
        try:
            model = json.loads(raw).get('model')
        except (ValueError, UnicodeError, AttributeError):
            return self.reply(400, 'invalid_json')
        if model not in self.server.models:
            return self.reply(400, 'model_not_allowed')
        if not self.server.slots.acquire(blocking=False):
            return self.reply(429, 'bridge_busy')
        started, upstream, sent = time.monotonic(), None, False
        try:
            upstream = http.client.HTTPConnection(*self.server.upstream, timeout=300)
            headers = {'Content-Type': 'application/json', 'Accept': self.headers.get('Accept', '*/*')}
            token = read_secret(self.server.token_file)
            if token:
                headers['Authorization'] = 'Bearer ' + token
            upstream.request('POST', self.path, body=raw, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP:
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            sent = True
            while data := response.read1(65536):
                self.wfile.write(data)
                self.wfile.flush()
            self.close_connection = True
            self.event(status=response.status, model=model, route=self.path,
                       seconds=round(time.monotonic() - started, 2))
        except (OSError, http.client.HTTPException):
            if not sent:
                self.reply(502, 'upstream_unavailable')
            self.close_connection = True
        finally:
            self.server.slots.release()
            if upstream:
                upstream.close()

    def do_GET(self):
        self.reply(405, 'method_not_allowed')

    do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address=LISTEN, upstream=UPSTREAM, peers=None, key_file=KEY_FILE,
                 token_file=TOKEN_FILE, models=MODELS, slots=4):
        if upstream[0] != '127.0.0.1':
            raise ValueError('upstream must be loopback')
        super().__init__(address, Handler)
        self.upstream, self.key_file, self.token_file = upstream, key_file, token_file
        self.peers = peers or Peers()
        self.models = frozenset(models)
        self.slots = threading.BoundedSemaphore(slots)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--init', action='store_true', help='create the bridge key file and exit')
    args = parser.parse_args()
    if args.init:
        ensure_key()
        return
    models = tuple(m for m in os.environ.get('FIRECRAWL_CORE_LLM_MODELS', ','.join(MODELS)).split(',') if m)
    if not read_secret(KEY_FILE):
        raise SystemExit('bridge key missing; run with --init first')
    with Server(models=models) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
