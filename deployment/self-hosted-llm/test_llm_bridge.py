import http.client
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

import llm_bridge as bridge

KEY = 'fc-llm-test-key'


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.calls.append((self.path, json.loads(body), self.headers.get('Authorization')))
        raw = json.dumps({'output': [{'type': 'message', 'content': [{'text': '{"a":1}'}]}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = tempfile.TemporaryDirectory()
        cls.key_file = Path(cls.state.name) / 'bridge-key'
        cls.key_file.write_text(KEY + '\n')
        cls.token_file = Path(cls.state.name) / 'token'
        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeUpstream)
        cls.upstream.calls = []
        cls.peers = bridge.Peers(lookup=lambda: set(cls.allowed))
        cls.allowed = {'127.0.0.1'}
        cls.server = bridge.Server(('127.0.0.1', 0), cls.upstream.server_address, cls.peers,
                                   cls.key_file, cls.token_file, ('gpt-6-luna--fast',))
        for server in (cls.upstream, cls.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.upstream):
            server.shutdown()
            server.server_close()
        cls.state.cleanup()
    def setUp(self):
        self.upstream.calls.clear()
        self.peers.loaded = 0.0
        type(self).allowed = {'127.0.0.1'}
        if self.token_file.exists():
            self.token_file.unlink()
    def call(self, path='/v1/responses', body=None, auth='Bearer ' + KEY, content_type='application/json',
             method='POST'):
        raw = json.dumps({'model': 'gpt-6-luna--fast', 'input': 'x'} if body is None else body).encode()
        c = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {'Content-Type': content_type}
        if auth:
            headers['Authorization'] = auth
        c.request(method, path, body=raw if method == 'POST' else None, headers=headers)
        r = c.getresponse()
        status, data = r.status, json.loads(r.read())
        c.close()
        return status, data

    def test_allowed_request_reaches_upstream_with_host_token_not_bridge_key(self):
        self.token_file.write_text('host-token\n')
        for path in bridge.PATHS:
            status, data = self.call(path)
            self.assertEqual(status, 200)
            self.assertEqual(data['output'][0]['content'][0]['text'], '{"a":1}')
        self.assertEqual([c[0] for c in self.upstream.calls], list(bridge.PATHS))
        self.assertEqual({c[2] for c in self.upstream.calls}, {'Bearer host-token'})

    def test_missing_host_token_sends_no_authorization(self):
        self.assertEqual(self.call()[0], 200)
        self.assertIsNone(self.upstream.calls[-1][2])

    def test_rejections_never_reach_upstream(self):
        cases = [
            ({'auth': None}, 401, 'invalid_bridge_key'),
            ({'auth': 'Bearer wrong'}, 401, 'invalid_bridge_key'),
            ({'path': '/v1/models'}, 404, 'route_not_allowed'),
            ({'path': '/v1/embeddings'}, 404, 'route_not_allowed'),
            ({'content_type': 'text/plain'}, 415, 'json_required'),
            ({'body': {'model': 'gpt-6-sol', 'input': 'x'}}, 400, 'model_not_allowed'),
            ({'body': ['not', 'an', 'object']}, 400, 'invalid_json'),
            ({'method': 'GET'}, 405, 'method_not_allowed'),
        ]
        for args, status, code in cases:
            with self.subTest(args=args):
                got, data = self.call(**args)
                self.assertEqual((got, data['error']['message']), (status, code))
        self.assertEqual(self.upstream.calls, [])

    def test_only_the_core_container_may_connect(self):
        type(self).allowed = {'172.20.0.6'}
        status, data = self.call()
        self.assertEqual((status, data['error']['message']), (403, 'peer_not_allowed'))
        self.assertEqual(self.upstream.calls, [])

    def test_peer_lookup_refreshes_after_container_recreate(self):
        addresses = [{'172.20.0.6'}, {'172.20.0.9'}]
        peers = bridge.Peers(lookup=lambda: addresses[0], ttl=30)
        self.assertTrue(peers.allowed('172.20.0.6'))
        addresses[0] = {'172.20.0.9'}
        peers.loaded -= 3  # The new address is looked up once the 2-second bound passes.
        self.assertTrue(peers.allowed('172.20.0.9'))
        self.assertFalse(peers.allowed('172.20.0.6'))

    def test_upstream_down_is_a_visible_error(self):
        server = bridge.Server(('127.0.0.1', 0), ('127.0.0.1', 1), self.peers, self.key_file,
                               self.token_file, ('gpt-6-luna--fast',))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection(*server.server_address, timeout=5)
            c.request('POST', '/v1/responses', body=b'{"model":"gpt-6-luna--fast"}',
                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read())['error']['message']), (502, 'upstream_unavailable'))
            c.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_init_creates_private_key_once(self):
        path = Path(self.state.name) / 'nested' / 'key'
        first = bridge.ensure_key(path)
        self.assertTrue(first.startswith('fc-llm-'))
        self.assertEqual(bridge.ensure_key(path), first)

    def test_upstream_must_be_loopback(self):
        with self.assertRaises(ValueError):
            bridge.Server(('127.0.0.1', 0), ('10.0.0.1', 10100), self.peers, self.key_file)


if __name__ == '__main__':
    unittest.main()
