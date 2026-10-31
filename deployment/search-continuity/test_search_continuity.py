import concurrent.futures
import http.client
import http.server
import json
import os
import sys
import tempfile
import importlib.util
from pathlib import Path
from unittest import mock
import threading
import unittest
from unittest.mock import Mock

import firecrawl_proxy as proxy
import search_continuity as policy

GOOD = {'url': 'https://example.org/doc', 'title': 'Source', 'description': 'Evidence'}


class PolicyTests(unittest.TestCase):
    def run_search(self, primary, fallback=None, **args):
        return policy.search({'query': 'public subject', 'limit': 3, **args}, primary,
                             fallback or Mock(return_value=[GOOD]), policy.FallbackGate())

    def test_normal_primary_keeps_metadata_and_no_fallback(self):
        value = {'success': True, 'data': {'web': [GOOD]}, 'id': 'original', 'creditsUsed': 2}
        fallback = Mock()
        result = self.run_search(Mock(return_value=value), fallback)
        self.assertEqual(result['id'], 'original')
        self.assertEqual(result['creditsUsed'], 2)
        self.assertFalse(result['searchContinuity']['fallback'])
        self.assertNotIn('searchContinuity', value)
        fallback.assert_not_called()

    def test_ambiguous_empty_is_not_declared_antibot(self):
        primary = Mock(return_value={'success': True, 'data': {}})
        result = self.run_search(primary)
        self.assertEqual([r['url'] for r in result['data']['web']], [GOOD['url']])
        self.assertEqual(result['searchContinuity']['primaryStatus'], 'primary_empty_unknown')
        self.assertEqual(primary.call_args.args[0]['excludeDomains'], list(policy.BAIDU))

    def test_error_falls_back_once(self):
        for primary in (Mock(side_effect=OSError()), Mock(return_value={'success': False})):
            fallback = Mock(return_value=[GOOD])
            self.assertEqual(self.run_search(primary, fallback)['searchContinuity']['provider'], 'exa')
            primary.assert_called_once()
            fallback.assert_called_once()

    def test_explicit_zero_is_honest_when_fallback_fails(self):
        result = self.run_search(Mock(return_value={'success': True, 'data': {'web': []}}),
                                 Mock(side_effect=policy.SearchFailure('exa_rate_limited', 429)))
        self.assertEqual(result['data']['web'], [])
        self.assertEqual(result['searchContinuity']['fallbackError'], 'exa_rate_limited')

    def test_both_fail_never_success(self):
        for response in ({'success': True, 'data': {}}, {'success': False}, None):
            with self.subTest(response=response), self.assertRaises(policy.SearchFailure):
                self.run_search(Mock(return_value=response), Mock(side_effect=OSError()))

    def test_auth_or_bad_request_cannot_activate_fallback(self):
        fallback = Mock()
        for status in (400, 401, 403, 404):
            with self.assertRaises(policy.SearchFailure):
                self.run_search(Mock(side_effect=policy.SearchFailure('primary_request_rejected', status)), fallback)
        fallback.assert_not_called()

    def test_true_no_results_can_be_empty(self):
        result = self.run_search(Mock(return_value={'success': True, 'data': {'web': []}}), Mock(return_value=[]))
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['web'], [])
        self.assertEqual(result['searchContinuity']['provider'], 'exa')

    def test_baidu_variants_filtered_and_lookalikes_preserved(self):
        bad = ['https://baidu.com/', 'https://BAIKE.BAIDU.COM./x', 'https://baidu.cn/',
               'https://x.baidu.hk/', 'https://b%61idu.com/', 'https://baidu\u3002com/']
        for url in bad:
            self.assertFalse(policy.allowed_url(url), url)
        for url in ['https://notbaidu.com/', 'https://baidu.com.example.org/']:
            self.assertTrue(policy.allowed_url(url), url)
        result = self.run_search(Mock(return_value={'success': True, 'data': {'web': [{'url': u} for u in bad] + [GOOD]}}))
        self.assertEqual([r['url'] for r in result['data']['web']], [GOOD['url']])

    def test_explicit_baidu_targets_rejected_before_io(self):
        for target in ['baidu.com/a', '//baike.baidu.com/a', 'https://BAIDU.COM./', 'https://b%61idu.com/', 'https://user:pass@baidu.com/']:
            with self.assertRaises(policy.SearchFailure):
                policy.reject_baidu_targets({'urls': [target]})
        for key in ('engine', 'provider'):
            with self.assertRaises(policy.SearchFailure):
                policy.reject_baidu_targets({key: 'baidu'})
        policy.reject_baidu_targets({'query': 'Baidu company history'})

    def test_exa_results_filtered_and_include_domains_preserved(self):
        fallback = Mock(return_value=[{'url': 'https://baidu.com/'}, GOOD, {'url': 'https://other.org'}])
        result = self.run_search(Mock(return_value={'success': True, 'data': {}}), fallback,
                                 includeDomains=['example.org'])
        self.assertEqual([r['url'] for r in result['data']['web']], [GOOD['url']])

    def test_specialized_search_does_not_drop_constraints_for_exa(self):
        for option in [{'country': 'DE'}, {'tbs': 'qdr:d'}, {'sources': ['images']},
                       {'scrapeOptions': {'formats': ['markdown']}}, {'categories': ['research']}]:
            fallback = Mock()
            primary = Mock(return_value={'success': True, 'data': {}})
            result = self.run_search(primary, fallback, **option)
            self.assertEqual(result['searchContinuity']['fallbackSkipped'], 'unsupported_search_constraints')
            for k, v in option.items():
                self.assertEqual(primary.call_args.args[0][k], v)
            fallback.assert_not_called()

    def test_cli_plain_web_metadata_is_compatible(self):
        self.assertTrue(policy.fallback_compatible({'query': 'q', 'limit': 1,
            'sources': [{'type': 'web'}], 'integration': 'cli', 'origin': 'js-sdk@4.44.0', 'toolDetail': 'compact',
            'domainTools': False, 'highlights': False, 'ignoreInvalidURLs': False}))
        self.assertFalse(policy.fallback_compatible({'query': 'q', 'highlights': True}))

    def test_mcp_default_tool_discovery_kept_with_explicit_web_only_fallback(self):
        original_tools = [{'name': 'existing_tool'}]
        result = self.run_search(Mock(return_value={'success': True, 'data': {'tools': original_tools}}),
            sources=['web', 'alexandria'], domainTools=True, origin='mcp-client')
        self.assertEqual(result['data']['tools'], original_tools)
        self.assertEqual(result['data']['web'][0]['url'], GOOD['url'])
        self.assertTrue(result['searchContinuity']['partialCapabilities'])
        self.assertIn('tool discovery not supplied by Exa', result['data']['web'][0]['description'])

    def test_exa_quota_metadata_without_iserror_starts_cooldown(self):
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
            '_meta': {'ai.exa/rateLimited': True},
            'content': [{'type': 'text', 'text': "You've hit Exa's free MCP rate limit."}]}}).encode()
        gate = policy.FallbackGate()
        fetch = Mock(side_effect=lambda q, n: policy.parse_exa(raw, n))
        for _ in range(2):
            with self.assertRaises(policy.SearchFailure) as caught:
                gate.call(fetch, 'public subject', 1)
            self.assertEqual((caught.exception.code, caught.exception.status), ('exa_rate_limited', 429))
        fetch.assert_called_once()

    def test_exa_mcp_rate_limit_is_not_empty_success(self):
        raw = json.dumps({'id': 1, 'result': {'isError': True,
              'content': [{'type': 'text', 'text': 'Rate limit exceeded'}]}}).encode()
        with self.assertRaises(policy.SearchFailure) as caught:
            policy.parse_exa(raw, 1)
        self.assertEqual(caught.exception.status, 429)

    def test_rate_limit_cooldown_makes_no_extra_external_calls(self):
        gate = policy.FallbackGate()
        fallback = Mock(side_effect=policy.SearchFailure('exa_rate_limited', 429))
        for _ in range(2):
            with self.assertRaises(policy.SearchFailure) as caught:
                gate.call(fallback, 'query', 1)
            self.assertEqual(caught.exception.status, 429)
        fallback.assert_called_once()

    def test_concurrent_fallback_bounded_and_slots_recovered(self):
        gate = policy.FallbackGate()
        release, both = threading.Event(), threading.Barrier(3)
        def blocking(q, n):
            both.wait(timeout=2)
            release.wait(timeout=2)
            return [GOOD]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(gate.call, blocking, 'q', 1) for _ in range(2)]
            both.wait(timeout=2)
            with self.assertRaisesRegex(policy.SearchFailure, 'exa_busy'):
                gate.call(Mock(), 'third', 1)
            release.set()
            self.assertTrue(all(j.result() == [GOOD] for j in jobs))
        self.assertEqual(gate.call(lambda q, n: [], 'q', 1), [])

    def test_exa_sse_and_explicit_zero_and_malformed(self):
        def reply(text):
            return json.dumps({'id': 1, 'result': {'content': [{'type': 'text', 'text': text}]}}).encode()
        text = 'Title: Source\nURL: https://example.org/doc\nEvidence'
        self.assertEqual(policy.parse_exa(b'data: ' + reply(text) + b'\n\n', 1)[0]['url'], GOOD['url'])
        self.assertEqual(policy.parse_exa(reply('No results found.'), 1), [])
        for raw in [reply('quota exhausted'), b'{}', b'not json', reply('')]:
            with self.assertRaises(policy.SearchFailure):
                policy.parse_exa(raw, 1)


class FakeBackend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(size)
        self.server.calls.append((self.path, body, self.headers.get('Authorization')))
        self.server.hosts.append(self.headers.get('Host'))
        if getattr(self.server, 'response_factory', None):
            status, value = self.server.response_factory(self.path, json.loads(body))
        elif self.path in ('/v1/search', '/v2/search'):
            value = {'success': True, 'data': {}}
        else:
            value = {'success': True, 'data': {'markdown': 'original page', 'echo': json.loads(body)}}
        raw = json.dumps(value).encode()
        self.send_response(status if 'status' in locals() else 200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Original', 'preserved')
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        self.send_response(307)
        self.send_header('Location', 'https://example.org/unchanged')
        self.send_header('Content-Length', '0')
        self.end_headers()


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeBackend)
        cls.backend.calls = []
        cls.backend.hosts = []
        cls.fallback = Mock(return_value=[GOOD])
        cls.server = proxy.Server(('127.0.0.1', 0), cls.backend.server_address, cls.fallback)
        for server in (cls.backend, cls.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.backend):
            server.shutdown()
            server.server_close()
    def setUp(self):
        self.enterContext(mock.patch.object(policy.controls.CONTROL.budget, 'run', side_effect=lambda fn: fn()))
        self.backend.response_factory = None

    def request(self, path, body=None, method='POST'):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json', 'Authorization': 'test-local-only'})
        r = c.getresponse()
        status, headers, data = r.status, dict(r.getheaders()), r.read()
        c.close()
        return status, headers, json.loads(data) if data else None
    def test_official_api_shape_and_canonical_route_variants(self):
        for path in ['/v2/search', '/V2/SEARCH/', '/v2/%73earch']:
            status, _, d = self.request(path, {'query': 'q', 'limit': 1})
            self.assertEqual(status, 200)
            self.assertEqual(d['data']['web'][0]['url'], GOOD['url'])
            self.assertIn('Search provider: Exa', d['data']['web'][0]['description'])
            self.assertEqual(d['searchContinuity']['provider'], 'exa')
    def test_scrape_options_bytes_and_authorization_stay_local(self):
        body = {'url': 'https://example.org/', 'formats': ['markdown'], 'fastMode': True, 'maxAge': 0}
        status, headers, d = self.request('/v2/scrape', body)
        self.assertEqual(d['data']['echo'], body)
        self.assertEqual(headers['X-Original'], 'preserved')
        self.assertEqual(self.backend.calls[-1][2], 'test-local-only')
    def test_client_host_reaches_backend_so_job_urls_stay_reachable(self):
        # Firecrawl builds crawl/batch status URLs from Host. Through the SSH tunnel the
        # client calls 127.0.0.1:13002; a backend Host of 127.0.0.1:3002 sent the MCP
        # crawl poll to a port that does not exist on the client.
        c = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        c.request('POST', '/v2/crawl', body=b'{"url": "https://example.org/"}',
                  headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:13002'})
        self.assertEqual(c.getresponse().status, 200)
        c.close()
        self.assertEqual(self.backend.hosts[-1], '127.0.0.1:13002')
    def test_reject_baidu_before_backend(self):
        before = len(self.backend.calls)
        self.assertEqual(self.request('/v2/scrape', {'url': 'https://baike.baidu.com/'})[0], 400)
        self.assertEqual(len(self.backend.calls), before)
    def test_backend_redirect_returned_not_followed(self):
        status, headers, _ = self.request('/v2/crawl/job', method='GET')
        self.assertEqual(status, 307)
        self.assertEqual(headers['Location'], 'https://example.org/unchanged')
    def test_parallel_requests_are_isolated(self):
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(self.request, '/v2/scrape', {'url': 'https://example.org/', 'marker': i}) for i in range(12)]
            values = [f.result()[2]['data']['echo']['marker'] for f in futures]
        self.assertEqual(values, list(range(12)))
    def test_json_chunked_request_is_supported(self):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        c.request('POST', '/v2/search', body=iter([b'{"query":', b'"q","limit":1}']),
                  headers={'Content-Type': 'application/json'}, encode_chunked=True)
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(json.loads(r.read())['searchContinuity']['fallback'])
        c.close()

    def test_ambiguous_framing_rejected_before_backend(self):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        c.putrequest('POST', '/v2/search')
        c.putheader('Content-Length', '2')
        c.putheader('Transfer-Encoding', 'chunked')
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        self.assertEqual(json.loads(r.read())['error'], 'ambiguous_body_framing')
        c.close()

    def test_large_search_scrape_result_keeps_markdown(self):
        rows = [{**GOOD, 'markdown': 'x' * 150000} for _ in range(20)]
        self.backend.response_factory = lambda path, body: (200, {'success': True, 'data': {'web': rows}})
        status, _, data = self.request('/v2/search', {'query': 'q', 'limit': 20, 'scrapeOptions': {'formats': ['markdown']}})
        self.assertEqual(status, 200)
        self.assertEqual(len(data['data']['web']), 20)
        self.assertEqual(len(data['data']['web'][0]['markdown']), 150000)

    def test_large_json_batch_request_is_forwarded(self):
        body = {'url': 'https://example.org', 'fixture': 'x' * 3000000}
        status, _, data = self.request('/v2/batch/scrape', body)
        self.assertEqual(status, 200)
        self.assertEqual(data['data']['echo'], body)

    def test_v1_search_filters_baidu_and_preserves_array_contract(self):
        self.backend.response_factory = lambda path, body: (200, {'success': True, 'data': [{'url': 'https://baidu.com/'}, GOOD]})
        status, _, data = self.request('/v1/search', {'query': 'q'})
        self.assertEqual(status, 200)
        self.assertEqual(data['data'], [GOOD])

    def test_backend_validation_details_preserved(self):
        payload = {'success': False, 'error': 'Bad Request', 'details': [{'path': ['limit'], 'message': 'invalid'}]}
        self.backend.response_factory = lambda path, body: (400, payload)
        status, _, data = self.request('/v2/search', {'query': 'q'})
        self.assertEqual(status, 400)
        self.assertEqual(data, payload)

    def test_backslash_parser_disagreement_cannot_reach_backend(self):
        before = len(self.backend.calls)
        status, _, _ = self.request('/v2/scrape', {'url': 'https://baidu.com\\@example.org/'})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.backend.calls), before)

    def test_non_string_search_urls_are_dropped(self):
        self.backend.response_factory = lambda path, body: (200, {'success': True, 'data': {'web': [{'url': 123}, GOOD]}})
        self.assertEqual(self.request('/v2/search', {'query': 'q'})[2]['data']['web'], [GOOD])

    def test_timeout_derived_with_margin(self):
        self.assertGreaterEqual(proxy.backend_timeout({'timeout': 90000}), 120)
        self.assertGreaterEqual(proxy.backend_timeout({'timeout': 300000}), 330)
        self.assertGreaterEqual(proxy.backend_timeout({'scrapeOptions': {'timeout': 300000}}), 330)
        self.assertGreaterEqual(proxy.backend_timeout(), 180)

    def test_absolute_url_never_proxies_to_external_host(self):
        self.assertEqual(self.request('https://baidu.com/v2/search', {'query': 'q'})[0], 400)


class FakeService(http.server.BaseHTTPRequestHandler):
    """Records every call; replies through the server's factory or a plain echo."""
    def log_message(self, *args): pass
    def handle_any(self):
        size = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(size)) if size else None
        self.server.calls.append({'method': self.command, 'path': self.path, 'body': body,
                                  'auth': self.headers.get('Authorization'), 'host': self.headers.get('Host')})
        status, value = self.server.factory(self.command, self.path, body)
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any


def echo(method, path, body):
    return 200, {'success': True, 'echo': {'method': method, 'path': path}}


class ServiceRouteTests(unittest.TestCase):
    """Monitor and browser routes reach their own authenticated GA401 services."""
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.servers = {}
        for name in ('core', 'monitor', 'browser'):
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeService)
            server.calls, server.factory = [], echo
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.servers[name] = server
        services = {}
        for name in ('monitor', 'browser'):
            key_file = Path(cls.keys.name) / (name + '.json')
            key_file.write_text(json.dumps({'api_key': 'fc-' + name + '-key'}))
            services[name] = (cls.servers[name].server_address, key_file)
        cls.server = proxy.Server(('127.0.0.1', 0), cls.servers['core'].server_address, Mock(), services)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for server in [cls.server, *cls.servers.values()]:
            server.shutdown()
            server.server_close()
        cls.keys.cleanup()
    def setUp(self):
        for server in self.servers.values():
            server.calls.clear()
            server.factory = echo
    def request(self, method, path, body=None):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json', 'Authorization': 'Bearer client-value',
                           'Host': '127.0.0.1:13002'})
        r = c.getresponse()
        status, data = r.status, r.read()
        c.close()
        return status, json.loads(data) if data else None
    def calls(self, name):
        return self.servers[name].calls

    def test_monitor_routes_reach_monitor_service_with_its_key(self):
        for method, path in [('GET', '/v2/monitor?limit=5'), ('POST', '/v2/monitor'),
                             ('GET', '/v2/monitor/abc/checks'), ('DELETE', '/V2/Monitor/abc')]:
            with self.subTest(path=path):
                status, data = self.request(method, path, {} if method == 'POST' else None)
                self.assertEqual(status, 200)
                self.assertEqual(data['echo'], {'method': method, 'path': path})
                call = self.calls('monitor')[-1]
                self.assertEqual(call['auth'], 'Bearer fc-monitor-key')
                self.assertEqual(call['host'], '127.0.0.1:13002')
        self.assertEqual(self.calls('core'), [])
        self.assertEqual(self.calls('browser'), [])

    def test_browser_and_interact_aliases_reach_browser_service(self):
        for path in ['/v2/browser', '/v2/browser/s1/execute', '/v2/interact', '/v2/interact/s1']:
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path, {})[0], 200)
                self.assertEqual(self.calls('browser')[-1]['path'], path)
                self.assertEqual(self.calls('browser')[-1]['auth'], 'Bearer fc-browser-key')
        self.assertEqual(self.calls('core'), [])

    def test_lookalike_paths_stay_on_core(self):
        for path in ['/v2/monitoring', '/v2/browsers', '/v1/monitor', '/v2/scrape/abc']:
            self.request('GET', path)
        self.assertEqual([c['path'] for c in self.calls('core')],
                         ['/v2/monitoring', '/v2/browsers', '/v1/monitor', '/v2/scrape/abc'])
        self.assertEqual(self.calls('core')[0]['auth'], 'Bearer client-value')
        self.assertEqual(self.calls('monitor') + self.calls('browser'), [])

    def test_missing_service_key_fails_before_any_backend_call(self):
        original = self.server.services['monitor']
        self.server.services['monitor'] = (original[0], Path(self.keys.name) / 'absent.json')
        try:
            status, data = self.request('GET', '/v2/monitor')
        finally:
            self.server.services['monitor'] = original
        self.assertEqual((status, data['error']), (503, 'local_service_unconfigured'))
        self.assertEqual(self.calls('monitor'), [])

    def test_interact_replays_core_scrape_once_on_browser_service(self):
        self.servers['core'].factory = lambda m, p, b: (200, {'success': True, 'data': {
            'markdown': 'page', 'json': {'a': 1}, 'metadata': {'scrapeId': 'Core-Scrape-1'}}})
        def browser(method, path, body):
            if path == '/v2/scrape':
                return 200, {'success': True, 'data': {'metadata': {'scrapeId': 'browser-7'}}}
            return echo(method, path, body)
        self.servers['browser'].factory = browser
        scrape = {'url': 'https://example.org/', 'formats': ['markdown', 'json'],
                  'jsonOptions': {'prompt': 'x'}, 'waitFor': 500, 'proxy': 'stealth'}
        status, data = self.request('POST', '/v2/scrape', scrape)
        self.assertEqual((status, data['data']['json']), (200, {'a': 1}))
        for _ in range(2):
            status, data = self.request('POST', '/v2/scrape/Core-Scrape-1/interact', {'prompt': 'click'})
            self.assertEqual(status, 200)
            self.assertEqual(data['echo']['path'], '/v2/scrape/browser-7/interact')
        status, data = self.request('DELETE', '/v2/scrape/Core-Scrape-1/interact')
        self.assertEqual(data['echo'], {'method': 'DELETE', 'path': '/v2/scrape/browser-7/interact'})
        replays = [c for c in self.calls('browser') if c['path'] == '/v2/scrape']
        self.assertEqual([c['body'] for c in replays], [{'url': 'https://example.org/', 'formats': ['markdown'],
                                                         'blockAds': False, 'maxAge': 0, 'waitFor': 500}])
        self.assertEqual(replays[0]['auth'], 'Bearer fc-browser-key')
        self.assertEqual(self.calls('browser')[1]['body'], {'prompt': 'click'})

    def test_unknown_scrape_id_passes_through_to_browser_service(self):
        status, data = self.request('POST', '/v2/scrape/native-id/interact', {'code': '1'})
        self.assertEqual(data['echo']['path'], '/v2/scrape/native-id/interact')
        self.assertEqual([c['path'] for c in self.calls('browser')], ['/v2/scrape/native-id/interact'])

    def test_failed_replay_returns_browser_error_and_is_not_cached(self):
        self.servers['core'].factory = lambda m, p, b: (200, {'success': True, 'data': {'metadata': {'scrapeId': 'core-2'}}})
        self.servers['browser'].factory = lambda m, p, b: (400, {'success': False, 'error': 'unsupported option'})
        self.request('POST', '/v2/scrape', {'url': 'https://example.org/'})
        for _ in range(2):
            status, data = self.request('POST', '/v2/scrape/core-2/interact', {'prompt': 'x'})
            self.assertEqual((status, data['error']), (400, 'unsupported option'))
        self.assertEqual([c['path'] for c in self.calls('browser')], ['/v2/scrape', '/v2/scrape'])

    def test_recent_map_expires_and_stays_bounded(self):
        recent = proxy.Recent(size=2, ttl=60)
        for key in 'abc':
            recent.put(key, key)
        self.assertEqual((recent.get('a'), recent.get('c')), (None, 'c'))
        with mock.patch.object(proxy.time, 'monotonic', return_value=proxy.time.monotonic() + 61):
            self.assertIsNone(recent.get('c'))


if __name__ == '__main__':
    unittest.main()
