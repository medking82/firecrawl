"""Search-only absolute budgets and fixed-destination transport, stdlib only.

All participating services run on the same Linux kernel monotonic clock.
The private header can only shorten the local ceiling. No redirects/proxies.
A watchdog shuts down active sockets even during headers, TLS or a read that
keeps receiving bytes. DNS runs in a reaped subprocess so cancellation does
not leave an uninterruptible resolver thread behind.
"""
import http.client
import ipaddress
import json
import math
import select
import socket
import ssl
import subprocess
import sys
import threading
import time

HEADER = 'X-Search-Deadline'
TOTAL_SECONDS = 25.0
PRIMARY_SECONDS = 12.5
EXA_HOST = 'mcp.exa.ai'


def deadline_header(value, ceiling):
    try:
        candidate = float(value)
        if math.isfinite(candidate):
            return min(candidate, ceiling)
    except (ValueError, TypeError):
        pass
    return ceiling


def stop_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


class Budget:
    def __init__(self, seconds=TOTAL_SECONDS, *, deadline=None, client=None, parent=None):
        self.deadline = min(time.monotonic() + seconds,
                            deadline if deadline is not None else float('inf'),
                            parent.deadline if parent else float('inf'))
        self.parent, self.client = parent, client
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.resources = []
        self.reason = None
        self.worker = threading.Thread(target=self._watch, name='search-budget', daemon=True)
        self.worker.start()

    def _watch(self):
        while not self.done.wait(.02):
            if self.parent and self.parent.cancelled.is_set():
                self.cancel(self.parent.reason)
                return
            if time.monotonic() >= self.deadline:
                self.cancel('search_deadline')
                return
            if self.client:
                try:
                    ready, _, _ = select.select([self.client], [], [], 0)
                    if ready and not self.client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        self.cancel('search_cancelled')
                        return
                except (OSError, ValueError):
                    self.cancel('search_cancelled')
                    return

    def cancel(self, reason):
        with self.lock:
            if self.cancelled.is_set():
                return
            self.reason = reason
            self.cancelled.set()
            callbacks = list(self.resources)
        for close in callbacks:
            try:
                close()
            except OSError:
                pass

    def track(self, close):
        with self.lock:
            self.resources.append(close)
            stopped = self.cancelled.is_set() or self.done.is_set()
        if stopped:
            close()
        self.remaining()

    def untrack(self, close):
        with self.lock:
            if close in self.resources:
                self.resources.remove(close)

    def remaining(self):
        if self.parent and self.parent.cancelled.is_set():
            self.cancel(self.parent.reason)
        if time.monotonic() >= self.deadline:
            self.cancel('search_deadline')
        if self.cancelled.is_set():
            raise TimeoutError(self.reason)
        return max(.001, self.deadline - time.monotonic())

    def close(self):
        self.done.set()
        if threading.current_thread() is not self.worker:
            self.worker.join(.2)
        with self.lock:
            callbacks, self.resources = self.resources, []
        for close in callbacks:
            try:
                close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def headers(self):
        self.remaining()
        return {HEADER: format(self.deadline, '.9f')}


# A separate process is intentional: libc getaddrinfo cannot be cancelled in a
# Python thread. Fixed host only; no query or credentials enter process args.
DNS_CODE = ('import json,socket; print(json.dumps(socket.getaddrinfo('
            '"mcp.exa.ai",443,socket.AF_INET,socket.SOCK_STREAM)))')


def public_address(rows):
    """Accept only an all-global IPv4 answer; mixed answers are rejected."""
    if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
        raise ValueError('exa_dns_rejected')
    return rows[0][4][0]


def resolve_exa(budget):
    budget.remaining()
    process = subprocess.Popen([sys.executable, '-I', '-c', DNS_CODE],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def terminate():
        if process.poll() is None:
            process.kill()
    try:
        budget.track(terminate)
        raw, _ = process.communicate(timeout=budget.remaining())
        budget.remaining()
        if process.returncode or len(raw) > 65536:
            raise ValueError('exa_dns_failed')
        return public_address(json.loads(raw))
    except subprocess.TimeoutExpired:
        budget.cancel('search_deadline')
        raise TimeoutError('search_deadline') from None
    finally:
        terminate()
        process.communicate()
        budget.untrack(terminate)


class LoopbackConnection(http.client.HTTPConnection):
    def __init__(self, port, budget):
        super().__init__('127.0.0.1', port, timeout=budget.remaining())
        self.budget = budget

    def connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock = sock
        self.budget.track(lambda: stop_socket(sock))
        sock.settimeout(self.budget.remaining())
        sock.connect(('127.0.0.1', self.port))


class PinnedExa(http.client.HTTPSConnection):
    def __init__(self, budget):
        super().__init__(EXA_HOST, 443, timeout=budget.remaining(), context=ssl.create_default_context())
        self.budget = budget

    def connect(self):
        address = resolve_exa(self.budget)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        close_raw = lambda: stop_socket(sock)
        try:
            self.budget.track(close_raw)
            sock.settimeout(self.budget.remaining())
            sock.connect((address, 443))  # validated numeric address; no second resolution
            secured = self._context.wrap_socket(sock, server_hostname=EXA_HOST,
                                                 do_handshake_on_connect=False)
            self.sock = secured
            self.budget.track(lambda: stop_socket(secured))
            self.budget.untrack(close_raw)
            secured.settimeout(self.budget.remaining())
            secured.do_handshake()
            self.budget.remaining()
        except BaseException:
            sock.close()
            if self.sock:
                self.sock.close()
            raise


def post(connection, path, payload, budget, maximum, headers=None):
    try:
        connection.request('POST', path, json.dumps(payload).encode(), {
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'Cache-Control': 'no-cache', **(headers or {})})
        response = connection.getresponse()
        chunks, size = [], 0
        while True:
            budget.remaining()
            part = response.read1(min(65536, maximum + 1 - size))
            budget.remaining()
            if not part:
                break
            size += len(part)
            if size > maximum:
                raise ValueError('response_too_large')
            chunks.append(part)
        return response.status, response.getheaders(), b''.join(chunks)
    except (OSError, ValueError, http.client.HTTPException):
        # A watchdog-closed socket surfaces as OSError/ValueError: report it as
        # deadline/cancellation, otherwise keep the original transport error.
        budget.remaining()
        raise
    finally:
        connection.close()


def exa_search(query, limit, continuity, budget, user_agent='johorindustry-public-web/1.0'):
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'web_search_exa', 'arguments': {'query': query, 'numResults': min(limit, 5),
        'objective': 'Return direct source URLs and evidence. Exclude baidu.com, baidu.cn, baidu.hk and their subdomains.'}}}
    try:
        status, _, raw = post(PinnedExa(budget), '/mcp', payload, budget, continuity.MAX_BODY, {
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-06-18', 'User-Agent': user_agent})
        if status != 200:
            raise continuity.SearchFailure('exa_rate_limited' if status == 429 else 'exa_http_error',
                                           429 if status == 429 else 503)
        return continuity.parse_exa(raw, min(limit, 5))
    except TimeoutError:
        raise continuity.SearchFailure(budget.reason or 'search_deadline', 504) from None
    except (OSError, ValueError, http.client.HTTPException) as error:
        if isinstance(error, continuity.SearchFailure):
            raise
        raise continuity.SearchFailure('exa_transport_failed') from None
