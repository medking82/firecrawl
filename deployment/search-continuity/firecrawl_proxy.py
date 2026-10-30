"""Optional loopback compatibility endpoint; official MCP/SDK/CLI stay intact.

No installation side effects. Start only with an explicit unused loopback port.
Non-search routes stream to the fixed local Firecrawl backend without redirects.
"""
import argparse
import http.client
import http.server
import json
import socket
import threading
import urllib.parse

import search_continuity as policy

BACKEND = ('127.0.0.1', 3002)
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}
MAX_JSON = 64 * 1024 * 1024


class BackendReply(Exception):
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw


def backend_timeout(body=None):
    values = [180.0]
    if isinstance(body, dict):
        for options in (body, body.get("scrapeOptions", {})):
            if isinstance(options, dict):
                value = options.get("timeout")
                if type(value) in (int, float) and 0 < value <= 86400000:
                    values.append(value / 1000.0 + 30)
    return max(values)


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
        skip = HOP | {'host', 'content-length', 'accept-encoding'}
        skip |= {x.strip().lower() for x in self.headers.get('Connection', '').split(',')}
        return {k: v for k, v in self.headers.items() if k.lower() not in skip}

    def primary(self, body):
        connection = http.client.HTTPConnection(*self.server.backend, timeout=backend_timeout(body))
        try:
            raw = json.dumps(body).encode()
            headers = self.forward_headers()
            headers['Content-Type'] = 'application/json'
            connection.request('POST', getattr(self, 'search_path', '/v2/search'), body=raw, headers=headers)
            response = connection.getresponse()
            if 400 <= response.status < 500 and response.status not in (408, 429):
                raw_error = response.read(MAX_JSON + 1)
                if len(raw_error) > MAX_JSON:
                    raise policy.SearchFailure('backend_error_response_too_large', 502)
                raise BackendReply(response.status, response.getheaders(), raw_error)
            if response.status != 200:
                raise policy.SearchFailure('primary_rate_limited' if response.status == 429 else 'primary_http_error',
                                           429 if response.status == 429 else 503)
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise policy.SearchFailure('primary_response_too_large')
            result = json.loads(raw)
            if getattr(self, 'search_path', '/v2/search') == '/v1/search' and isinstance(result, dict) and isinstance(result.get('data'), list):
                result['data'] = {'web': result['data']}
            return result
        except (http.client.HTTPException, OSError):
            raise policy.SearchFailure('primary_unavailable') from None
        finally:
            connection.close()

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
                    result = policy.controls.CONTROL.run(body,
                        lambda: policy.search(body, self.primary, fallback=self.server.fallback,
                                              gate=self.server.fallback_gate),
                        fresh='no-cache' in self.headers.get('Cache-Control', '').lower())
                    if path == '/v1/search':
                        result['data'] = result['data'].get('web', [])
                    self.reply(200, result)
                    return
            # All other existing REST routes remain on the original backend.
            # Stream both uploads and replies; keep response bytes/status intact.
            connection = http.client.HTTPConnection(*self.server.backend, timeout=backend_timeout(body))
            headers = self.forward_headers()
            if raw is None and self.headers.get('Content-Length') is not None:
                headers['Content-Length'] = self.headers['Content-Length']
            payload = bytes(raw) if raw is not None else (parts if self.headers.get('Content-Length') or self.headers.get('Transfer-Encoding') else None)
            connection.request(self.command, self.path, body=payload, headers=headers,
                               encode_chunked=raw is None and 'Content-Length' not in headers)
            response = connection.getresponse()
            self.send_response(response.status)
            skip = HOP | {'server', 'date'}
            skip |= {x.strip().lower() for x in response.getheader('Connection', '').split(',')}
            for key, value in response.getheaders():
                if key.lower() not in skip:
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            sent = True
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
    def __init__(self, address, backend=BACKEND, fallback=policy.exa_search):
        if address[0] != '127.0.0.1' or backend[0] != '127.0.0.1':
            raise ValueError('Only fixed loopback listeners/backends are allowed')
        super().__init__(address, Handler)
        self.backend, self.fallback = backend, fallback
        self.fallback_gate = policy.FallbackGate()
        self.slots = threading.BoundedSemaphore(16)

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
