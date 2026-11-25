"""Optional loopback compatibility endpoint; official MCP/SDK/CLI stay intact.

No installation side effects. Start only with an explicit unused loopback port.
Non-search routes stream to the fixed local Firecrawl backend without redirects.
Monitor and browser/interact routes go to the separate GA401 monitor and browser
services, which enforce database authentication; the proxy adds their local key.
"""
import argparse
import collections
import http.client
import http.server
import json
import re
import socket
import threading
import time
import urllib.parse
from pathlib import Path

import importlib.util

import search_continuity as policy


def _load_transport():
    """Root/owner-installed sibling in a release; the shared repo copy in tests."""
    here = Path(__file__).resolve().parent
    for path in (here / 'search_transport.py', here.parent.parent / 'hermes-johorindustry' / 'search_transport.py'):
        if path.is_file() and not path.is_symlink():
            spec = importlib.util.spec_from_file_location('strata_search_transport', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError('Search transport sibling unavailable')


transport = _load_transport()

BACKEND = ('127.0.0.1', 3002)
# The core searches through the sidecar worker when its override points the
# SearXNG provider there (deployment/search-sidecar); only then may the query
# carry search hints. Rolling the override back turns hints off with it.
COMPOSE_OVERRIDE = Path('/home/marck/github/firecrawl/docker-compose.override.yaml')
SIDECAR_ENDPOINT = re.compile(r'(?m)^\s*SEARXNG_ENDPOINT:\s*["\']?http://search-worker:8081/?["\']?\s*$')
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}
MAX_JSON = 64 * 1024 * 1024
SHARED = Path.home() / 'services'
# Local services whose private client.json holds the API key of their own tenant.
SERVICES = {
    'monitor': (('127.0.0.1', 3006), SHARED / 'firecrawl-monitor/shared/client.json'),
    'browser': (('127.0.0.1', 3007), SHARED / 'firecrawl-browser/shared/client.json'),
}
INTERACT = re.compile(r'/v2/scrape/([^/]+)/interact', re.IGNORECASE)
# Options the browser service replays; it fails closed on proxies, profiles and the like.
REPLAY_OPTIONS = ('waitFor', 'actions', 'headers', 'timeout', 'skipTlsVerification')
INTERACT_TIMEOUT = 360.0


class BackendReply(Exception):
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw


def legacy_search_route_missing(raw):
    if b'<pre>Cannot POST /v1/search</pre>' in raw:
        return True
    try:
        error = json.loads(raw)
    except (ValueError, UnicodeError):
        return False
    return (isinstance(error, dict) and error.get('code') == 'NOT_FOUND'
            and error.get('error') == 'POST /v1/search is not a Firecrawl API endpoint.')


def removed_no_cache_fields(raw, options):
    try:
        reply = json.loads(raw)
    except (ValueError, UnicodeError):
        return []
    details = reply.get('details') if isinstance(reply, dict) and reply.get('code') == 'BAD_REQUEST' else None
    if not isinstance(details, list) or len(details) != 1 or not isinstance(details[0], dict):
        return []
    issue = details[0]
    keys = issue.get('keys')
    if (issue.get('code') != 'unrecognized_keys' or issue.get('path') != []
            or not isinstance(keys, list) or not keys
            or any(not isinstance(key, str) or key not in options for key in keys)):
        return []
    return keys


def service_route(path):
    for name, prefixes in (('monitor', ('/v2/monitor',)), ('browser', ('/v2/browser', '/v2/interact'))):
        if any(path == p or path.startswith(p + '/') for p in prefixes):
            return name
    return None


def service_key(path):
    try:
        value = json.loads(Path(path).read_text()).get('api_key')
    except (OSError, ValueError, AttributeError):
        value = None
    if not isinstance(value, str) or not value:
        raise policy.SearchFailure('local_service_unconfigured', 503)
    return value


def replay_request(body):
    """Browser-service scrape that loads the page a core scrape fetched."""
    replay = {'url': body['url'], 'formats': ['markdown'], 'blockAds': False, 'maxAge': 0}
    replay.update({k: body[k] for k in REPLAY_OPTIONS if k in body})
    return replay


class Recent:
    """Bounded in-memory map with expiry; 24 hours matches browser scrape access."""
    def __init__(self, size=2048, ttl=86400):
        self.size, self.ttl = size, ttl
        self.items = collections.OrderedDict()
        self.lock = threading.Lock()

    def put(self, key, value):
        with self.lock:
            self.items.pop(key, None)
            self.items[key] = (time.monotonic() + self.ttl, value)
            while len(self.items) > self.size:
                self.items.popitem(last=False)

    def get(self, key):
        with self.lock:
            row = self.items.get(key)
            if row and row[0] > time.monotonic():
                return row[1]
            self.items.pop(key, None)
            return None


def backend_timeout(body=None):
    values = [180.0]
    if isinstance(body, dict):
        for options in (body, body.get("scrapeOptions", {})):
            if isinstance(options, dict):
                value = options.get("timeout")
                if type(value) in (int, float) and 0 < value <= 86400000:
                    values.append(value / 1000.0 + 30)
    return max(values)


def search_seconds(body):
    """One absolute ceiling per search request.

    Plain web search (the only fallback-compatible shape) is capped at the
    shared transport total and may be shortened by the client timeout.
    scrapeOptions searches keep their scrape budget: no fallback exists for
    them, but the budget is still absolute and cancelled on disconnect.
    """
    if isinstance(body, dict) and body.get('scrapeOptions'):
        return backend_timeout(body), False
    seconds = transport.TOTAL_SECONDS
    value = body.get('timeout') if isinstance(body, dict) else None
    if type(value) in (int, float) and value > 0:
        seconds = min(seconds, max(value / 1000.0, 1.0))
    return seconds, True


def read_bounded(response, budget, maximum, code, status):
    """Read with the budget checked between parts; a watchdog-closed socket is a timeout."""
    chunks, size = [], 0
    try:
        while True:
            budget.remaining()
            part = response.read1(min(65536, maximum + 1 - size))
            budget.remaining()
            if not part:
                return b''.join(chunks)
            size += len(part)
            if size > maximum:
                raise policy.SearchFailure(code, status)
            chunks.append(part)
    except policy.SearchFailure:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        budget.remaining()
        raise


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # Never log raw paths, query text, credentials or response content.

    def setup(self):
        super().setup()
        self.connection.settimeout(120)

    def reply(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        if status == 429:
            self.send_header('Retry-After', '60')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)
        self.close_connection = True

    def chunks(self):
        """Decode chunk framing with bounded chunks; no unbounded read or trailer reuse."""
        while True:
            line = self.rfile.readline(128)
            if not line.endswith(b'\r\n') or b';' in line:
                raise policy.SearchFailure('invalid_chunk_framing', 400)
            try:
                size = int(line.strip(), 16)
            except ValueError:
                raise policy.SearchFailure('invalid_chunk_framing', 400) from None
            if size < 0 or size > 16 * 1024 * 1024:
                raise policy.SearchFailure('invalid_chunk_size', 400)
            if not size:
                if self.rfile.readline(3) != b'\r\n':
                    raise policy.SearchFailure('request_trailers_unsupported', 400)
                return
            left = size
            while left:
                value = self.rfile.read(min(left, 65536))
                if not value:
                    raise policy.SearchFailure('truncated_body', 400)
                left -= len(value)
                yield value
            if self.rfile.read(2) != b'\r\n':
                raise policy.SearchFailure('invalid_chunk_framing', 400)

    def body_parts(self):
        lengths = self.headers.get_all('Content-Length', [])
        transfers = self.headers.get_all('Transfer-Encoding', [])
        if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
            raise policy.SearchFailure('ambiguous_body_framing', 400)
        if transfers:
            if transfers[0].lower() != 'chunked':
                raise policy.SearchFailure('unsupported_transfer_encoding', 400)
            return self.chunks()
        try:
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError()
        except ValueError:
            raise policy.SearchFailure('invalid_content_length', 400) from None
        def parts():
            left = length
            while left:
                value = self.rfile.read(min(left, 65536))
                if not value:
                    raise policy.SearchFailure('truncated_body', 400)
                left -= len(value)
                yield value
        return parts()

    def forward_headers(self):
        # Host passes through (already checked to be loopback): Firecrawl builds job
        # status URLs from it, and clients reach this proxy on another port (13002).
        skip = HOP | {'content-length', 'accept-encoding', transport.HEADER.lower()}
        skip |= {x.strip().lower() for x in self.headers.get('Connection', '').split(',')}
        return {k: v for k, v in self.headers.items() if k.lower() not in skip}

    def primary(self, body):
        parent = getattr(self, 'search_budget', None)
        stage = transport.Budget(getattr(self, 'primary_seconds', transport.PRIMARY_SECONDS), parent=parent) \
            if parent is not None else transport.Budget(backend_timeout(body))
        connection = None
        try:
            path = getattr(self, 'search_path', '/v2/search')
            connection = transport.LoopbackConnection(self.server.backend[1], stage)
            headers = self.forward_headers()
            headers['Content-Type'] = 'application/json'
            connection.request('POST', path, body=json.dumps(body).encode(), headers=headers)
            response = connection.getresponse()
            if path == '/v1/search' and response.status == 404:
                missing_route = read_bounded(response, stage, MAX_JSON, 'backend_error_response_too_large', 502)
                if not legacy_search_route_missing(missing_route):
                    raise BackendReply(response.status, response.getheaders(), missing_route)
                connection.close()
                connection = transport.LoopbackConnection(self.server.backend[1], stage)
                request = {'limit': 5, **body, 'sources': ['web']}
                request.pop('__searchPreviewToken', None)
                connection.request('POST', '/v2/search', body=json.dumps(request).encode(), headers=headers)
                response = connection.getresponse()
            if 400 <= response.status < 500 and response.status not in (408, 429):
                raw_error = read_bounded(response, stage, MAX_JSON, 'backend_error_response_too_large', 502)
                raise BackendReply(response.status, response.getheaders(), raw_error)
            if response.status != 200:
                raise policy.SearchFailure('primary_rate_limited' if response.status == 429 else 'primary_http_error',
                                           429 if response.status == 429 else 503)
            raw = read_bounded(response, stage, MAX_JSON, 'primary_response_too_large', 503)
            result = json.loads(raw)
            if path == '/v1/search' and isinstance(result, dict):
                if isinstance(result.get('data'), list):
                    result['data'] = {'web': result['data']}
                if 'jobId' in result and 'id' not in result:
                    result['id'] = result.pop('jobId')
            return result
        except TimeoutError:
            # Stage expiry still leaves the entry budget for fallback; entry
            # expiry/disconnect makes the fallback fail fast as well.
            if parent is not None and parent.cancelled.is_set():
                raise policy.SearchFailure(parent.reason or 'search_deadline', 504) from None
            raise policy.SearchFailure('primary_timeout', 504) from None
        except (http.client.HTTPException, OSError):
            raise policy.SearchFailure('primary_unavailable') from None
        finally:
            if connection is not None:
                connection.close()
            stage.close()

    def search_fallback(self, budget):
        configured = self.server.fallback
        if configured is policy.exa_search:
            # Same fixed Exa endpoint, now DNS-validated, TLS-verified and bound to the entry budget.
            return lambda query, limit: transport.exa_search(query, limit, policy, budget,
                                                             user_agent='strata-public-web/1.0')
        def injected(query, limit):
            try:
                budget.remaining()
                rows = configured(query, limit)
                budget.remaining()
                return rows
            except TimeoutError:
                raise policy.SearchFailure(budget.reason or 'search_deadline', 504) from None
        return injected

    def budgeted_search(self, body):
        seconds, plain = search_seconds(body)
        deadline = transport.deadline_header(self.headers.get(transport.HEADER), time.monotonic() + seconds)
        with transport.Budget(seconds, deadline=deadline, client=self.connection) as budget:
            self.search_budget = budget
            self.primary_seconds = transport.PRIMARY_SECONDS if plain else seconds
            fallback = self.search_fallback(budget)
            def operation():
                try:
                    budget.remaining()  # admission wait counts against the same ceiling
                    return policy.search(body, self.primary, fallback=fallback, gate=self.server.fallback_gate,
                                         domain_filters=getattr(self, 'search_path', '/v2/search') != '/v1/search',
                                         hints=self.server.search_hints())
                except TimeoutError:
                    raise policy.SearchFailure(budget.reason or 'search_deadline', 504) from None
            try:
                with policy.controls.request_budget(budget):
                    return policy.controls.CONTROL.run(
                        body, operation, fresh='no-cache' in self.headers.get('Cache-Control', '').lower())
            except TimeoutError:
                raise policy.SearchFailure(budget.reason or 'search_deadline', 504) from None
            finally:
                self.search_budget = None

    def remember_scrape(self, raw, body):
        if len(raw) > MAX_JSON:
            return
        try:
            scrape_id = json.loads(raw)['data']['metadata']['scrapeId']
        except (ValueError, UnicodeError, KeyError, TypeError):
            return
        if isinstance(scrape_id, str) and scrape_id:
            self.server.scrapes.put(scrape_id, replay_request(body))

    def interact_target(self, scrape_id, headers):
        """Map a core scrape ID to a browser-service scrape of the same page.

        The core API keeps no scrape context (database authentication is off), so
        the first interact loads the page once on the browser service and later
        calls reuse that scrape. IDs the proxy never saw pass through unchanged.
        """
        mapped = self.server.browser_scrapes.get(scrape_id)
        replay = self.server.scrapes.get(scrape_id)
        if mapped or self.command != 'POST' or replay is None:
            return mapped or scrape_id
        with self.server.replay_lock:
            mapped = self.server.browser_scrapes.get(scrape_id)
            if mapped:
                return mapped
            connection = http.client.HTTPConnection(*self.server.services['browser'][0],
                                                    timeout=backend_timeout(replay))
            try:
                connection.request('POST', '/v2/scrape', body=json.dumps(replay).encode(), headers={
                    **{k: v for k, v in headers.items() if k.lower() != 'content-type'},
                    'Content-Type': 'application/json'})
                response = connection.getresponse()
                result = response.read(MAX_JSON + 1)
                if response.status != 200:
                    raise BackendReply(response.status, response.getheaders(), result)
                try:
                    mapped = json.loads(result)['data']['metadata']['scrapeId']
                except (ValueError, UnicodeError, KeyError, TypeError):
                    mapped = None
                if not isinstance(mapped, str) or not mapped:
                    raise policy.SearchFailure('interact_replay_failed', 502)
            finally:
                connection.close()
            self.server.browser_scrapes.put(scrape_id, mapped)
            return mapped

    def handle_proxy(self):
        connection = None
        sent = False
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or not self.path.startswith('/') or self.path.startswith('//'):
                raise policy.SearchFailure('absolute_request_target_rejected', 400)
            if self.headers.get('Upgrade'):
                raise policy.SearchFailure('upgrade_not_supported', 400)
            host = urllib.parse.urlsplit('http://' + self.headers.get('Host', '')).hostname
            if host not in ('127.0.0.1', 'localhost'):
                raise policy.SearchFailure('loopback_host_required', 400)
            path = urllib.parse.unquote(parsed.path).rstrip('/').lower()
            if self.command == 'GET' and path == '/continuity/health':
                self.reply(200, {'success': True, 'service': 'firecrawl-search-continuity', 'backend': 'self-hosted'})
                return
            parts = self.body_parts()
            body = None
            is_json = 'application/json' in self.headers.get('Content-Type', '').lower()
            raw = None
            if is_json or (self.command == 'POST' and path in ('/v1/search', '/v2/search')):
                raw = bytearray()
                for part in parts:
                    raw.extend(part)
                    if len(raw) > MAX_JSON:
                        raise policy.SearchFailure('json_body_too_large', 413)
                try:
                    body = json.loads(raw) if raw else {}
                except (ValueError, UnicodeError):
                    raise policy.SearchFailure('invalid_json', 400) from None
                policy.reject_baidu_targets(body)
                if self.command == 'POST' and path in ('/v1/search', '/v2/search'):
                    self.search_path = path
                    result = self.budgeted_search(body)
                    if path == '/v1/search':
                        result['data'] = result['data'].get('web', [])
                    self.reply(200, result)
                    return
            # Other REST routes remain on the original backend, except monitor and
            # browser routes. Stream uploads and replies; keep response bytes/status intact.
            backend, target, timeout = self.server.backend, self.path, backend_timeout(body)
            headers = self.forward_headers()
            # Match before lowercasing so scrape IDs keep their case.
            interact = INTERACT.fullmatch(urllib.parse.unquote(parsed.path).rstrip('/'))
            service = 'browser' if interact else service_route(path)
            if service:
                backend, key_file = self.server.services[service]
                headers = {k: v for k, v in headers.items() if k.lower() != 'authorization'}
                headers['Authorization'] = 'Bearer ' + service_key(key_file)
            if interact:
                scrape_id = self.interact_target(interact.group(1), headers)
                target = '/v2/scrape/' + urllib.parse.quote(scrape_id, safe='') + '/interact'
                target += '?' + parsed.query if parsed.query else ''
                timeout = INTERACT_TIMEOUT
            record = (not service and self.command == 'POST' and path == '/v2/scrape'
                      and isinstance(body, dict) and isinstance(body.get('url'), str))
            connection = http.client.HTTPConnection(*backend, timeout=timeout)
            if raw is None and self.headers.get('Content-Length') is not None:
                headers['Content-Length'] = self.headers['Content-Length']
            payload = bytes(raw) if raw is not None else (parts if self.headers.get('Content-Length') or self.headers.get('Transfer-Encoding') else None)
            connection.request(self.command, target, body=payload, headers=headers,
                               encode_chunked=raw is None and 'Content-Length' not in headers)
            response = connection.getresponse()
            first = b''
            if record and response.status == 400:
                no_cache = set()
                if type(body.get('maxAge')) is int and body['maxAge'] == 0:
                    no_cache.add('maxAge')
                if body.get('storeInCache') is False:
                    no_cache.add('storeInCache')
                if no_cache:
                    first = response.read(MAX_JSON + 1)
                    removed = removed_no_cache_fields(first, no_cache)
                    if removed:
                        connection.close()
                        connection = http.client.HTTPConnection(*backend, timeout=timeout)
                        payload = json.dumps({key: value for key, value in body.items() if key not in removed}).encode()
                        connection.request(self.command, target, body=payload, headers=headers)
                        response = connection.getresponse()
                        first = b''
            if record and response.status == 200:
                # Buffer the scrape reply once to learn its ID; bytes are sent unchanged.
                first = response.read(MAX_JSON + 1)
                self.remember_scrape(first, body)
            self.send_response(response.status)
            skip = HOP | {'server', 'date'}
            skip |= {x.strip().lower() for x in response.getheader('Connection', '').split(',')}
            for key, value in response.getheaders():
                if key.lower() not in skip:
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            sent = True
            if first:
                self.wfile.write(first)
            if self.command != 'HEAD':
                while data := response.read1(65536):
                    self.wfile.write(data)
                    self.wfile.flush()
        except BackendReply as e:
            if not sent:
                self.send_response(e.status)
                for key, value in e.headers:
                    if key.lower() not in HOP | {'content-length', 'server', 'date'}:
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(e.raw)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(e.raw)
        except RecursionError:
            if not sent:
                self.reply(400, {'success': False, 'error': 'json_nesting_too_deep'})
        except policy.controls.BudgetBusy as e:
            if not sent:
                self.reply(429, {'success': False, 'error': str(e)})
        except policy.SearchFailure as e:
            if not sent:
                self.reply(e.status, {'success': False, 'error': e.code})
        except (OSError, http.client.HTTPException, ValueError):
            if not sent:
                self.reply(502, {'success': False, 'error': 'local_backend_unavailable'})
        except Exception:
            if not sent:
                self.reply(502, {'success': False, 'error': 'proxy_processing_failed'})
        finally:
            self.close_connection = True
            if connection:
                connection.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_proxy


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, backend=BACKEND, fallback=policy.exa_search, services=None):
        services = SERVICES if services is None else services
        if address[0] != '127.0.0.1' or backend[0] != '127.0.0.1' or any(
                target[0] != '127.0.0.1' for target, _ in services.values()):
            raise ValueError('Only fixed loopback listeners/backends are allowed')
        super().__init__(address, Handler)
        self.backend, self.fallback, self.services = backend, fallback, services
        self.scrapes, self.browser_scrapes = Recent(), Recent()
        self.replay_lock = threading.Lock()
        self.fallback_gate = policy.FallbackGate()
        self.slots = threading.BoundedSemaphore(16)
        self.hint_state = (None, False)

    def search_hints(self):
        try:
            info = COMPOSE_OVERRIDE.stat()
            key = (info.st_mtime_ns, info.st_size, info.st_ino)
            if self.hint_state[0] != key:
                self.hint_state = (key, bool(SIDECAR_ENDPOINT.search(COMPOSE_OVERRIDE.read_text())))
            return self.hint_state[1]
        except (OSError, UnicodeError):
            return False

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if args.port in (3002, 13002) or not 1024 <= args.port <= 65535:
        parser.error('Use a separate nonprivileged loopback port; existing service stays intact')
    with Server(('127.0.0.1', args.port)) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
