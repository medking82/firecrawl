import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import official_read as reader


def done(stdout='', stderr='', code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


PAGE = {'url': 'https://www.nmpa.gov.cn/xxgk/a/20261123163401193.html', 'title': '通告', 'text': '正文' * 10,
        'base64': 'PGh0bWw+', 'method': 'ga401_chrome',
        'links': [{'url': 'https://www.nmpa.gov.cn/a.docx', 'title': ' 附件 \n docx '},
                  {'url': 'https://www.nmpa.gov.cn/a.docx', 'title': 'dup'}, {'url': 'javascript:;', 'title': 'x'}]}


class Supported(unittest.TestCase):
    def test_hosts(self):
        self.assertTrue(reader.supported('https://www.nmpa.gov.cn/ylqx/index.html'))
        self.assertTrue(reader.supported('https://www.cde.org.cn/main/news'))
        for url in ('http://www.nmpa.gov.cn/', 'https://nmpa.gov.cn.evil.com/', 'https://u:p@www.nmpa.gov.cn/',
                    'https://www.nmpa.gov.cn:8443/', 'https://example.com/', 'file:///etc/passwd', 'not a url'):
            self.assertFalse(reader.supported(url), url)


class Read(unittest.TestCase):
    def test_runs_reader_as_forma_from_root(self):
        seen = {}
        def run(command, **kw):
            seen.update(command=command, **kw)
            return done(json.dumps(PAGE))
        self.assertEqual(reader.read(PAGE['url'], run=run)['title'], '通告')
        self.assertEqual(seen['command'][:4], ['sudo', '-n', '-u', 'forma'])
        self.assertEqual(seen['command'][-2:], [PAGE['url'], '9333'])
        self.assertEqual(seen['cwd'], '/')

    def test_rejects_before_running(self):
        def run(*a, **kw):
            raise AssertionError('must not run')
        with self.assertRaises(reader.ReadFailure) as ctx:
            reader.read('https://example.com/', run=run)
        self.assertEqual(ctx.exception.code, 'unsupported_host')

    def test_failures(self):
        cases = [(done(stderr='official_browser_homepage', code=1), 'official_browser_homepage'),
                 (done(stderr='Bad!!', code=1), 'reader_failed'), (done('not json'), 'reader_invalid_output'),
                 (done('{"title": 1}'), 'reader_invalid_output')]
        for result, code in cases:
            with self.assertRaises(reader.ReadFailure) as ctx:
                reader.read(PAGE['url'], run=lambda *a, result=result, **kw: result)
            self.assertEqual(ctx.exception.code, code)
        def slow(*a, **kw):
            raise subprocess.TimeoutExpired('node', 1)
        with self.assertRaises(reader.ReadFailure) as ctx:
            reader.read(PAGE['url'], run=slow)
        self.assertEqual(ctx.exception.code, 'reader_timeout')


class Output(unittest.TestCase):
    def test_markdown_paging_and_links(self):
        text = reader.markdown(PAGE, offset=0, max_chars=5)
        self.assertIn('Source: ' + PAGE['url'], text)
        self.assertIn('More content is available at offset 5.', text)
        self.assertEqual(text.count('a.docx'), 1)
        self.assertIn('- 附件 docx — https://www.nmpa.gov.cn/a.docx', text)
        self.assertNotIn('javascript', text)
        self.assertIn('End of retrieved content.', reader.markdown(PAGE))

    def test_cli_json_drops_html_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'lock'
            out = io.StringIO()
            with redirect_stdout(out):
                rc = reader.main([PAGE['url'], '--json'], run=lambda *a, **kw: done(json.dumps(PAGE)), lock_path=lock)
            self.assertEqual(rc, 0)
            self.assertNotIn('base64', json.loads(out.getvalue()))
            err = io.StringIO()
            with redirect_stderr(err):
                rc = reader.main(['https://example.com/'], lock_path=lock)
            self.assertEqual(rc, 2)
            self.assertIn('unsupported_host', err.getvalue())

    def test_lock_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'lock'
            ticks = iter(range(1000))
            with reader.serialized(lock):
                with self.assertRaises(reader.ReadFailure) as ctx:
                    with reader.serialized(lock, wait=3, clock=lambda: next(ticks), sleep=lambda s: None):
                        pass
            self.assertEqual(ctx.exception.code, 'reader_busy')


if __name__ == '__main__':
    unittest.main()
