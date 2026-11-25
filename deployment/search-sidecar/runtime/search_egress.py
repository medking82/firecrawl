#!/usr/bin/env python3
"""Fixed-host CONNECT egress for the isolated search worker; no HTTP forwarding."""
import ipaddress
import json
import re
import selectors
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from typing import cast

ALLOWED = frozenset(('search.brave.com', 'search.yahoo.com', 'duckduckgo.com',
                     'html.duckduckgo.com', 'links.duckduckgo.com'))
# DuckDuckGo's GeoDNS can hand this network a single frontend that is unreachable
# while other frontends serve normally. For these hosts only, merge several DNS
# sources, probe each address over verified TLS for the DuckDuckGo page, and
# remember which addresses work. DuckDuckGo publishes no frontend address list,
# so DNS is the only discovery source. Client TLS stays end to end, so a
# candidate address can only pick a server, never vouch for one.
DDG = frozenset(('duckduckgo.com', 'html.duckduckgo.com', 'links.duckduckgo.com'))
DOH = ('https://8.8.8.8/resolve?name={}&type=A', 'https://1.1.1.1/dns-query?name={}&type=A')
# Frontends observed serving html.duckduckgo.com on 2026-11-24; last resort only,
# and only for the hosts they were observed serving (links.* is another service).
SEED = ('20.204.244.192', '52.142.124.215', '40.114.177.156', '52.250.42.157')
SEED_HOSTS = frozenset(('duckduckgo.com', 'html.duckduckgo.com'))
# Probes fetch the query-less page: no search is sent, so probing cannot spend
# the search quota or trip the anti-bot check that real queries are subject to.
PROBE = {'html.duckduckgo.com': '/html/', 'duckduckgo.com': '/'}
POOL_TTL = 300
PROBE_INTERVAL = 1800
KEEP_SECONDS = 86400
BAD_SECONDS = 600
HEALTH = {}
POOL = {}
STATE = threading.Lock()
TRACE = re.compile(r'[a-zA-Z0-9_-]{16,64}\Z')

def target(authority):
    if authority not in {host + ':443' for host in ALLOWED}:
        raise ValueError('destination_rejected')
    return authority[:-4]

def public_addresses(records):
    result = []
    for value in records:
        ip = ipaddress.ip_address(value)
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or getattr(ip, 'ipv4_mapped', None):
            raise ValueError('dns_private_or_mixed')
        if str(ip) not in result:
            result.append(str(ip))
    if not result or len(result) > 32:
        raise ValueError('dns_invalid')
    return result

def resolve(host):
    # Killing and reaping the helper also bounds libc DNS, which socket timeouts do not.
    p = subprocess.run([sys.executable, '-I', '-B', __file__, '--resolve', host],
                       capture_output=True, text=True, timeout=2, check=True)
    if len(p.stdout) > 8192:
        raise ValueError('dns_invalid')
    return public_addresses(json.loads(p.stdout))

def doh(host, url):
    request = urllib.request.Request(url.format(host), headers={'Accept': 'application/dns-json'})
    with urllib.request.urlopen(request, timeout=2) as response:
        body = response.read(16384)
    answers = json.loads(body).get('Answer') or []
    found = []
    for row in answers:
        if row.get('type') == 1:
            try: found.extend(public_addresses([row.get('data')]))
            except ValueError: continue
    return found

def probe(host, ip):
    """True: serves the DuckDuckGo page; False: unreachable or wrong; None: anti-bot page."""
    path = PROBE.get(host)
    body = bytearray()
    try:
        with socket.create_connection((ip, 443), timeout=2) as raw:
            raw.settimeout(3)
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                if path is None:
                    return True
                tls.sendall(('GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\n'
                             'Accept: text/html\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n'
                             % (path, host)).encode('ascii'))
                while len(body) < 65536:
                    chunk = tls.recv(16384)
                    if not chunk: break
                    body.extend(chunk)
    except (OSError, ValueError):
        return False
    parts = bytes(body[:32]).split(b' ')
    status = parts[1] if len(parts) > 1 and parts[0].startswith(b'HTTP/1.') else b''
    if b'anomaly-modal' in body:
        return None
    if host == 'html.duckduckgo.com':
        return status in (b'200', b'202') and b'search_form_homepage' in body
    return status == b'200'

def note(host, ip, ok, verified=False):
    with STATE:
        entry = HEALTH.setdefault((host, ip), {'ok': 0.0, 'verified': 0.0, 'bad': 0.0})
        now = time.monotonic()
        if ok:
            entry.update(ok=now, bad=0.0)
            if verified: entry['verified'] = now
        else:
            entry['bad'] = now + BAD_SECONDS

def known(host, now):
    with STATE:
        return [ip for (name, ip), entry in HEALTH.items()
                if name == host and entry['verified'] and now - entry['verified'] < KEEP_SECONDS]

def seeds(host):
    return list(SEED) if host in SEED_HOSTS else []

def refresh(host):
    found = []
    for url in DOH:
        try: found.extend(doh(host, url))
        except (OSError, ValueError): continue
    found = list(dict.fromkeys(found))[:16]
    now = time.monotonic()
    for ip in list(dict.fromkeys(found + known(host, now) + seeds(host)))[:12]:
        with STATE:
            entry = dict(HEALTH.get((host, ip), {'verified': 0.0, 'bad': 0.0}))
        if entry['verified'] and now - entry['verified'] < PROBE_INTERVAL and entry['bad'] <= now:
            continue
        result = probe(host, ip)
        if result is not None:
            note(host, ip, result, verified=result)
    with STATE:
        POOL[host] = {'expires': time.monotonic() + POOL_TTL, 'addresses': found, 'refreshing': False}
    event('ddg_pool_refreshed', uuid.uuid4().hex, host=host, dns=found, verified=known(host, time.monotonic()))

def pool(host):
    with STATE:
        entry = POOL.setdefault(host, {'expires': 0.0, 'addresses': [], 'refreshing': False})
        stale = entry['expires'] <= time.monotonic() and not entry['refreshing']
        if stale: entry['refreshing'] = True
        addresses = list(entry['addresses'])
    if stale:
        threading.Thread(target=refresh, args=(host,), daemon=True).start()
    return addresses

def candidates(host):
    try: system = resolve(host)
    except (OSError, ValueError, subprocess.SubprocessError): system = []
    now = time.monotonic()
    merged = list(dict.fromkeys(system + pool(host) + known(host, now) + seeds(host)))
    with STATE:
        health = {ip: dict(HEALTH.get((host, ip), {'ok': 0.0, 'verified': 0.0, 'bad': 0.0})) for ip in merged}
    good = sorted((ip for ip in merged if health[ip]['ok'] and health[ip]['bad'] <= now),
                  key=lambda ip: (-health[ip]['verified'], -health[ip]['ok']))
    untried = [ip for ip in merged if not health[ip]['ok'] and health[ip]['bad'] <= now]
    cooling = sorted((ip for ip in merged if health[ip]['bad'] > now), key=lambda ip: health[ip]['bad'])
    return (good + untried + cooling)[:8]

def dial(addresses, total=3, attempt=None, record=None):
    deadline = time.monotonic() + total
    for ip in sorted(addresses, key=lambda ip: ':' in ip):
        sock = socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM)
        try:
            remaining = max(.01, deadline - time.monotonic())
            sock.settimeout(min(remaining, attempt) if attempt else remaining)
            sock.connect((ip, 443))  # literal IP only: no second DNS lookup
            if record: note(record, ip, True)
            return sock
        except OSError:
            sock.close()
            if record: note(record, ip, False)
            if time.monotonic() >= deadline:
                break
    raise ValueError('connect_failed')

def connect(host):
    if host in DDG:
        return dial(candidates(host), total=5, attempt=1.2, record=host)
    return dial(resolve(host))

def event(name, trace, **values):
    print(json.dumps({'event': name, 'requestId': trace, **values}), flush=True)

def tunnel(client, upstream):
    """Drain a final upstream response; client EOF still cancels immediately."""
    end = time.monotonic() + 15
    last = time.monotonic()
    upstream_eof = False
    pending = {client: bytearray(), upstream: bytearray()}
    counts = {'clientBytes': 0, 'upstreamBytes': 0}
    for sock in pending:
        sock.setblocking(False)
    with selectors.DefaultSelector() as poll:
        while time.monotonic() < end:
            if upstream_eof and not pending[client]:
                return 'upstream_eof', counts
            for sock, peer in ((client, upstream), (upstream, client)):
                flags = (selectors.EVENT_READ if not upstream_eof and len(pending[peer]) < 65536 else 0)
                if pending[sock]: flags |= selectors.EVENT_WRITE
                if flags:
                    try: poll.modify(sock, flags)
                    except KeyError: poll.register(sock, flags)
                else:
                    try: poll.unregister(sock)
                    except KeyError: pass
            if time.monotonic() - last >= 5:
                return 'idle', counts
            for key, flags in poll.select(.2):
                sock = cast(socket.socket, key.fileobj)
                peer = upstream if sock is client else client
                if flags & selectors.EVENT_READ and not upstream_eof:
                    try: data = sock.recv(16384)
                    except BlockingIOError: continue
                    if not data:
                        if sock is client:
                            return 'client_eof', counts
                        upstream_eof = True
                        pending[upstream].clear()
                    else:
                        pending[peer].extend(data)
                        counts['clientBytes' if sock is client else 'upstreamBytes'] += len(data)
                        last = time.monotonic()
                if flags & selectors.EVENT_WRITE and pending[sock]:
                    try: n = sock.send(pending[sock])
                    except BlockingIOError: continue
                    del pending[sock][:n]
                    last = time.monotonic()
    return 'deadline', counts

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        trace = uuid.uuid4().hex
        reason = 'rejected'
        counts = {}
        host = None
        try:
            client.settimeout(3)
            raw = bytearray()
            header_end = time.monotonic() + 3
            while b'\r\n\r\n' not in raw and len(raw) <= 8192:
                if time.monotonic() >= header_end: raise ValueError('header_deadline')
                client.settimeout(max(.01, header_end - time.monotonic()))
                part = client.recv(1)  # do not consume any following TLS bytes
                if not part: return
                raw.extend(part)
            if len(raw) > 8192: raise ValueError('headers_too_large')
            lines = raw.decode('ascii').split('\r\n')
            method, authority, version = lines[0].split(' ')
            if method != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('method_rejected')
            host = target(authority)
            for line in lines[1:]:
                if line.lower().startswith('x-search-request-id:'):
                    candidate = line.split(':', 1)[1].strip()
                    if TRACE.fullmatch(candidate): trace = candidate
            upstream = connect(host)
            event('proxy_open', trace, host=host, client=list(self.client_address),
                  local=list(upstream.getsockname()), remote=list(upstream.getpeername()))
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            reason, counts = tunnel(client, upstream)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            if upstream is None:
                try: client.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
                except OSError: pass
        finally:
            for sock in (upstream, client):
                if sock is not None:
                    try: sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    sock.close()
            event('proxy_closed', trace, host=host, reason=reason, **counts)
            event('proxy_thread_exit', trace, thread=threading.current_thread().name)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def __init__(self, address, handler=Handler):
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, handler)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            cast(socket.socket, request).close()
            return
        try: super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.slots.release()

def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--resolve':
        if sys.argv[2] not in ALLOWED: raise SystemExit(2)
        print(json.dumps(sorted({a[4][0] for a in socket.getaddrinfo(sys.argv[2], 443, type=socket.SOCK_STREAM)})))
        return
    with Server(('0.0.0.0', 3128)) as server:
        server.serve_forever(poll_interval=.2)

if __name__ == '__main__': main()
