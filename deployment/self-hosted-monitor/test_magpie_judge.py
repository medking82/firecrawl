import http.server
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import magpie_judge  # noqa: E402

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['done'],
          'properties': {'done': {'type': 'boolean'}}}


class Fake(http.server.BaseHTTPRequestHandler):
    status, reply, seen = 200, {}, []

    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers['Content-Length'])
        Fake.seen.append((self.path, self.headers['Authorization'], json.loads(self.rfile.read(size))))
        raw = json.dumps(Fake.reply).encode()
        self.send_response(Fake.status)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def answer(text, extra=()):
    return {'status': 'completed', 'output': [{'type': 'reasoning'}, *extra,
            {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}


class MagpieJudge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Fake.status, Fake.reply, Fake.seen = 200, answer('{"done": true}'), []
        self.temp = tempfile.TemporaryDirectory()
        key = Path(self.temp.name) / 'key'
        key.write_text('secret-key\n')
        env = {'MAGPIE_JUDGE_TOKEN_FILE': str(key)}
        for name in ('MAGPIE_JUDGE_MODEL', 'MAGPIE_JUDGE_EFFORT'):
            env[name] = ''
        self.patches = [patch.object(magpie_judge, 'UPSTREAM', self.server.server_address),
                        patch.dict(os.environ, env)]
        for p in self.patches:
            p.start()
        for name in ('MAGPIE_JUDGE_MODEL', 'MAGPIE_JUDGE_EFFORT'):
            os.environ.pop(name)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_pins_transport_schema_and_effort(self):
        result = magpie_judge.judge('SYS', 'PROMPT', timeout=5, schema=SCHEMA)
        self.assertEqual(result, {'text': '{"done": true}', 'model': 'gpt-6.1-terra', 'backend': 'magpie',
                                  'tool_calls': 0, 'settings': {'model': 'gpt-6.1-terra',
                                                                'model_reasoning_effort': 'medium'}})
        path, auth, body = Fake.seen[0]
        self.assertEqual((path, auth), ('/v1/responses', 'Bearer secret-key'))
        self.assertEqual(body['model'], 'codex/gpt-6.1-terra')
        self.assertEqual(body['reasoning'], {'effort': 'medium'})
        self.assertEqual(body['text']['format']['schema'], SCHEMA)
        self.assertTrue(body['text']['format']['strict'])
        self.assertEqual((body['tools'], body['store'], body['input']), ([], False, 'PROMPT'))
        self.assertIn('untrusted evidence', body['instructions'])
        self.assertTrue(body['instructions'].endswith('SYS'))

    def test_environment_selects_model_and_effort(self):
        with patch.dict(os.environ, {'MAGPIE_JUDGE_MODEL': 'gpt-6.1-sol', 'MAGPIE_JUDGE_EFFORT': 'low'}):
            magpie_judge.judge('s', 'p', timeout=5, schema=SCHEMA)
        self.assertEqual(Fake.seen[0][2]['model'], 'codex/gpt-6.1-sol')
        self.assertEqual(Fake.seen[0][2]['reasoning'], {'effort': 'low'})

    def test_rejects_bad_settings_and_inputs_before_network(self):
        for kwargs in ({'model': 'codex/x'}, {'effort': 'max'}):
            with self.assertRaises(ValueError):
                magpie_judge.judge('s', 'p', timeout=5, schema=SCHEMA, **kwargs)
        with self.assertRaises(ValueError):
            magpie_judge.judge('s', 'x' * 130000, timeout=5, schema=SCHEMA)
        self.assertEqual(Fake.seen, [])

    def test_fails_closed_without_leaking_body(self):
        Fake.status, Fake.reply = 502, {'error': 'page evidence here'}
        with self.assertRaises(RuntimeError) as caught:
            magpie_judge.judge('s', 'p', timeout=5, schema=SCHEMA)
        self.assertEqual(str(caught.exception), 'magpie_http_502')
        for reply, error in ((answer('{}', [{'type': 'function_call'}]), RuntimeError),
                             ({'status': 'incomplete', 'output': []}, RuntimeError),
                             (answer('not json'), ValueError)):
            Fake.status, Fake.reply = 200, reply
            with self.assertRaises(error):
                magpie_judge.judge('s', 'p', timeout=5, schema=SCHEMA)

    def test_unreachable_gateway(self):
        with patch.object(magpie_judge, 'UPSTREAM', ('127.0.0.1', 9)):
            with self.assertRaises(RuntimeError):
                magpie_judge.judge('s', 'p', timeout=2, schema=SCHEMA)


if __name__ == '__main__':
    unittest.main()
