"""Real-HTTP deadline/cancellation regressions for the 3003 search path.

Each case uses a live loopback backend that misbehaves at the socket level
(stalled body, byte trickle, refused port) and measures wall time end to end.
"""
import http.client
import http.server
import json
import select
import socket
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import firecrawl_proxy as proxy  # noqa: E402

GOOD = {'url': 'https://example.org/a', 'title': 'A', 'description': 'd'}
PRIMARY, TOTAL = 1.0, 2.0


class Stalling(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        mode = self.server.mode
        start = time.monotonic()
        if mode in ('v1-removed', 'v1-error'):
            self.server.requests.append((self.path, body))
        if self.path == '/v1/search' and mode in ('v1-removed', 'v1-error'):
            error = ('POST /v1/search is not a Firecrawl API endpoint.' if mode == 'v1-removed'
                     else 'search not found')
            raw = json.dumps({'success': False, 'code': 'NOT_FOUND', 'error': error}).encode()
            self.send_response(404)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if mode in ('ok', 'v1-removed'):
            raw = json.dumps({'success': True, 'data': {'web': [GOOD]}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '100000')
        self.end_headers()
        self.wfile.flush()
        # Stall or trickle until the proxy closes our socket; record when it did.
        while time.monotonic() - start < 30:
            if mode == 'trickle':
                try:
                    self.wfile.write(b' ')
                    self.wfile.flush()
                except OSError:
                    break
            ready, _, _ = select.select([self.connection], [], [], .2)
            if ready:
                try:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        break
                except OSError:
                    break
        self.server.closed_after.append(time.monotonic() - start)


class DeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stalling)
        cls.backend.mode, cls.backend.closed_after = 'stall', []
        cls.backend.requests = []
        cls.fallback_rows = [GOOD]
        cls.fallback_calls = []

        def fallback(query, limit):
            cls.fallback_calls.append(query)
            if cls.fallback_rows is None:
                raise proxy.policy.SearchFailure('exa_http_error')
            return list(cls.fallback_rows)

        cls.server = proxy.Server(('127.0.0.1', 0), cls.backend.server_address, fallback)
        for server in (cls.backend, cls.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.temp = tempfile.TemporaryDirectory()
        control = proxy.policy.controls.SearchControls(budget=proxy.policy.controls.SharedBudget(
            root=Path(cls.temp.name) / 'budget', spacing=0))
        cls.patches = [mock.patch.object(proxy.policy.controls, 'CONTROL', control),
                       mock.patch.object(proxy.transport, 'PRIMARY_SECONDS', PRIMARY),
                       mock.patch.object(proxy.transport, 'TOTAL_SECONDS', TOTAL)]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        for server in (cls.server, cls.backend):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.backend.closed_after.clear()
        self.backend.requests.clear()
        self.fallback_calls.clear()
        type(self).fallback_rows = [GOOD]
        proxy.policy.controls.CONTROL.cache.clear()

    def search(self, query, headers=None, path='/v2/search'):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=30)
        raw = json.dumps({'query': query, 'limit': 3}).encode()
        start = time.monotonic()
        connection.request('POST', path, raw, {
            'Content-Type': 'application/json', 'Cache-Control': 'no-cache', **(headers or {})})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body, time.monotonic() - start

    def test_v1_search_uses_v2_when_core_removed_route(self):
        self.backend.mode = 'v1-removed'
        status, body, _ = self.search('legacy query', path='/v1/search')
        self.assertEqual(status, 200)
        self.assertEqual(body['data'], [GOOD])
        self.assertEqual(self.backend.requests, [
            ('/v1/search', {'query': 'legacy query', 'limit': 3}),
            ('/v2/search', {'query': 'legacy query', 'limit': 3, 'sources': ['web']}),
        ])
        self.assertEqual(self.fallback_calls, [])

    def test_v1_search_preserves_backend_404(self):
        self.backend.mode = 'v1-error'
        status, body, _ = self.search('legacy error query', path='/v1/search')
        self.assertEqual(status, 404)
        self.assertEqual(body['error'], 'search not found')
        self.assertEqual([path for path, _ in self.backend.requests], ['/v1/search'])

    def wait_backend_closed(self, limit=3.0):
        end = time.monotonic() + limit
        while not self.backend.closed_after and time.monotonic() < end:
            time.sleep(.02)
        self.assertTrue(self.backend.closed_after, 'backend socket never released')
        return self.backend.closed_after[-1]

    def test_stalled_body_falls_back_within_primary_stage(self):
        self.backend.mode = 'stall'
        status, body, elapsed = self.search('stalled body q')
        print('stalled_body_seconds', round(elapsed, 3))
        self.assertEqual(status, 200)
        self.assertEqual(body['searchContinuity']['provider'], 'exa')
        self.assertTrue(body['data']['web'])
        self.assertLess(elapsed, PRIMARY + .6)
        self.assertLess(self.wait_backend_closed(), PRIMARY + .6)

    def test_trickle_cannot_extend_primary(self):
        self.backend.mode = 'trickle'
        status, body, elapsed = self.search('trickle q')
        print('trickle_seconds', round(elapsed, 3))
        self.assertEqual(status, 200)
        self.assertEqual(body['searchContinuity']['provider'], 'exa')
        self.assertLess(elapsed, PRIMARY + .6)

    def test_all_sources_failed_is_bounded_error(self):
        self.backend.mode = 'stall'
        type(self).fallback_rows = None
        status, body, elapsed = self.search('all fail q')
        print('all_failed_seconds', round(elapsed, 3), status, body.get('error'))
        self.assertGreaterEqual(status, 500)
        self.assertIs(body['success'], False)
        self.assertNotIn('data', body)
        self.assertLess(elapsed, TOTAL + .6)

    def test_client_deadline_header_only_shortens(self):
        self.backend.mode = 'stall'
        type(self).fallback_rows = None
        status, body, elapsed = self.search('short deadline q', {
            proxy.transport.HEADER: format(time.monotonic() + .5, '.9f')})
        print('header_deadline_seconds', round(elapsed, 3), status)
        self.assertEqual(status, 504)
        self.assertLess(elapsed, 1.1)
        self.assertEqual(self.fallback_calls, [])  # entry deadline gone: no fallback work started

    def test_client_disconnect_cancels_backend_request(self):
        self.backend.mode = 'stall'
        with mock.patch.object(proxy.transport, 'PRIMARY_SECONDS', 20.0), \
                mock.patch.object(proxy.transport, 'TOTAL_SECONDS', 25.0):
            sock = socket.create_connection(self.server.server_address)
            raw = json.dumps({'query': 'disconnect q', 'limit': 3}).encode()
            sock.sendall(b'POST /v2/search HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
                         b'Cache-Control: no-cache\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw)
            time.sleep(.4)
            closed = time.monotonic()
            sock.close()
            released = self.wait_backend_closed(3.0)
            print('disconnect_release_seconds', round(time.monotonic() - closed, 3))
            self.assertLess(released, 2.0)
            time.sleep(.2)
            self.assertEqual(self.fallback_calls, [])

    def test_refused_backend_falls_back_fast(self):
        dead = socket.socket()
        dead.bind(('127.0.0.1', 0))
        port = dead.getsockname()[1]
        dead.close()
        with mock.patch.object(self.server, 'backend', ('127.0.0.1', port)):
            status, body, elapsed = self.search('refused q')
        print('refused_seconds', round(elapsed, 3))
        self.assertEqual(status, 200)
        self.assertEqual(body['searchContinuity']['provider'], 'exa')
        self.assertLess(elapsed, .6)

    def test_primary_ok_unchanged(self):
        self.backend.mode = 'ok'
        status, body, elapsed = self.search('ok q')
        self.assertEqual(status, 200)
        self.assertEqual(body['searchContinuity']['provider'], 'firecrawl')
        self.assertLess(elapsed, .6)

    def test_admission_spacing_cannot_extend_entry_deadline(self):
        self.backend.mode = 'ok'
        admission = proxy.policy.controls.CONTROL.budget
        # A previous admission imposes 2s spacing; this caller permits only .3s.
        with mock.patch.object(admission, 'spacing', 2.0):
            admission.run(lambda: None)
            status, body, elapsed = self.search('admission deadline q', {
                proxy.transport.HEADER: str(time.monotonic() + .3)})
        self.assertEqual(status, 504)
        self.assertLess(elapsed, .7)
        self.assertEqual(self.fallback_calls, [])
        print('admission_cancel_seconds', round(elapsed, 3))

    def test_scrape_options_keep_their_own_budget(self):
        seconds, plain = proxy.search_seconds({'query': 'x', 'scrapeOptions': {'formats': ['markdown']}})
        self.assertFalse(plain)
        self.assertGreaterEqual(seconds, 180)
        self.assertEqual(proxy.search_seconds({'query': 'x'}), (TOTAL, True))
        self.assertEqual(proxy.search_seconds({'query': 'x', 'timeout': 500}), (1.0, True))


if __name__ == '__main__':
    unittest.main()
