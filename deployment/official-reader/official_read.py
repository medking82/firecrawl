#!/usr/bin/env python3
"""Read an official regulator page through GA401's headed forma-cde-reader Chrome."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse

READER = '/home/marck/services/forma-v2-report-evidence/report-browser.mjs'
NODE = '/opt/hermes-tools/node-26.12.1-linux-x64/bin/node'
ACCOUNT = 'forma'
PORT = '9333'
TIMEOUT = 120
LOCK_WAIT = 150
HOSTS = ('nmpa.gov.cn', 'cde.org.cn', 'chinadrugtrials.org.cn', 'ema.europa.eu', 'pmda.go.jp', 'fda.gov',
         'globenewswire.com', 'sec.gov', 'businesswire.com', 'prnewswire.com')
MAX_LINKS = 40


class ReadFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def supported(url):
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or '').lower().rstrip('.')
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password and not parsed.port
            and any(host == item or host.endswith('.' + item) for item in HOSTS))


@contextlib.contextmanager
def serialized(path, wait=LOCK_WAIT, clock=time.monotonic, sleep=time.sleep):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        deadline = clock() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise ReadFailure('reader_busy')
                sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read(url, run=subprocess.run):
    if not supported(url):
        raise ReadFailure('unsupported_host')
    command = ['sudo', '-n', '-u', ACCOUNT, NODE, READER, url, PORT]
    try:
        done = run(command, cwd='/', capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ReadFailure('reader_timeout')
    if done.returncode != 0:
        code = (done.stderr or '').strip()
        raise ReadFailure(code if re.fullmatch(r'[a-z_]{1,100}', code) else 'reader_failed')
    try:
        page = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise ReadFailure('reader_invalid_output')
    if not isinstance(page, dict) or not isinstance(page.get('text'), str):
        raise ReadFailure('reader_invalid_output')
    return page


def markdown(page, offset=0, max_chars=20000):
    text = page['text']
    part = text[offset:offset + max_chars]
    end = offset + len(part)
    lines = [f"Source: {page.get('url', '')}", f"Title: {page.get('title', '')}",
             'Retrieved with GA401 headed Chrome (forma-cde-reader).', '', part.strip(), '']
    lines.append(f'More content is available at offset {end}.' if end < len(text) else 'End of retrieved content.')
    links, seen = [], set()
    for link in page.get('links') or []:
        href, title = link.get('url') or '', ' '.join((link.get('title') or '').split())
        if href.startswith('http') and title and href not in seen:
            seen.add(href)
            links.append((len(title) < 12, f'- {title} — {href}'))
    if links:
        links.sort(key=lambda item: item[0])
        lines += ['', 'Links:'] + [line for _, line in links[:MAX_LINKS]]
    return '\n'.join(lines)


def main(argv=None, run=subprocess.run, lock_path=None):
    parser = argparse.ArgumentParser(prog='official-read', description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--json', action='store_true', help='print the reader result as JSON')
    parser.add_argument('--html', action='store_true', help='with --json, keep the base64 page HTML')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--max-chars', type=int, default=20000)
    args = parser.parse_args(argv)
    lock_path = lock_path or Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'official-reader.lock'
    try:
        with serialized(lock_path):
            page = read(args.url, run=run)
    except ReadFailure as failure:
        print(f'official-read failed: {failure.code}', file=sys.stderr)
        return 2
    if args.json:
        if not args.html:
            page.pop('base64', None)
        print(json.dumps(page, ensure_ascii=False))
    else:
        print(markdown(page, max(0, args.offset), max(1, min(200000, args.max_chars))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
