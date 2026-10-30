"""GA401 shared search admission and in-memory, authenticated-session reuse.

No network, credentials, model calls, query logs or persistent result cache.
The shared lock records only a monotonic admission timestamp.
"""
import contextlib
import contextvars
import copy
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from collections import OrderedDict

STATE = Path('/home/marck/.local/state/strata-search-budget')
SCOPE = contextvars.ContextVar('search_session_scope', default=None)
FRESH = contextvars.ContextVar('search_fresh', default=False)


class BudgetBusy(ValueError):
    pass


@contextlib.contextmanager
def trusted_session(account, session):
    # Called only AFTER gateway session_account authenticated this pair.
    scope = None
    if isinstance(account, str) and account and isinstance(session, str) and session:
        scope = hashlib.sha256(json.dumps([account, session]).encode()).digest()
    token = SCOPE.set(scope)
    try:
        yield
    finally:
        SCOPE.reset(token)


def wants_fresh(text):
    return bool(re.search(r'\b(latest|current|today|now|live|real.time|breaking)\b|最新|当前|現在|现在|今天|今日|实时|即時', str(text), re.I))


@contextlib.contextmanager
def freshness(value):
    token = FRESH.set(bool(value) or FRESH.get())
    try:
        yield
    finally:
        FRESH.reset(token)


class SharedBudget:
    """One search workflow at a time across GA processes; no upstream retry.

    A held OS flock survives neither process exit nor crash. Bounded admission
    wait is not an upstream request. Windows test/remote clients run on GA proxy.
    """
    def __init__(self, root=STATE, spacing=2.0, wait=8.0):
        self.root, self.spacing, self.wait = Path(root), spacing, wait

    def run(self, operation):
        if os.name != 'posix':
            return operation()  # This server's production owner is Linux GA401.
        import fcntl
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError('search_budget_unsafe_path')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('search_budget_unsafe_owner')
        path = self.root / 'admission.lock'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info, named = os.fstat(descriptor), os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError('search_budget_unsafe_lock')
            deadline = time.monotonic() + self.wait
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BudgetBusy('shared_search_budget_busy') from None
                    time.sleep(min(.025, max(0, deadline - time.monotonic())))
            raw = os.read(descriptor, 128)
            try:
                previous = float(raw) if raw else 0.0
            except ValueError:
                raise ValueError('search_budget_invalid_state') from None
            if not math.isfinite(previous):
                raise ValueError("search_budget_invalid_state")
            now = time.monotonic()
            delay = max(0.0, previous + self.spacing - now) if 0 <= previous <= now else 0.0
            if now + delay > deadline:
                raise BudgetBusy('shared_search_budget_busy')
            if delay:
                time.sleep(delay)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, str(time.monotonic()).encode())
            os.ftruncate(descriptor, os.lseek(descriptor, 0, os.SEEK_CUR))
            return operation()
        finally:
            os.close(descriptor)


class SearchControls:
    def __init__(self, budget=None, ttl=30.0, capacity=128, clock=time.monotonic, wall=time.time):
        self.budget = budget if budget is not None else SharedBudget()
        self.ttl, self.capacity, self.clock, self.wall = ttl, capacity, clock, wall
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.flights = {}

    def _marked(self, value, stamp, retrieved, state):
        result = copy.deepcopy(value)
        info = result.setdefault('searchContinuity', {})
        info.update(retrievedAt=retrieved, cacheAgeSeconds=round(max(0.0, self.clock() - stamp), 3),
                    cacheStatus=state)
        return result

    @staticmethod
    def _eligible(value):
        if not isinstance(value, dict) or value.get('success') is not True:
            return False
        info, data = value.get('searchContinuity', {}), value.get('data', {})
        # Do not reuse failures, ambiguous primary empties or fallback warnings.
        return (isinstance(info, dict) and info.get('provider') == 'firecrawl'
                and info.get('fallback') is False and not info.get('warning')
                and isinstance(data, dict) and isinstance(data.get('web'), list) and bool(data['web']))

    def run(self, body, operation, *, scope=None, fresh=False):
        if not isinstance(body, dict):
            return operation()
        scope = SCOPE.get() if scope is None else scope
        fresh = fresh or FRESH.get() or wants_fresh(body.get('query', '')) or bool(body.get('tbs'))
        key = None
        if isinstance(scope, bytes) and scope and not fresh:
            # Complete request semantics; never fold case, drop options, or use client-supplied identity.
            encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
            key = (scope, hashlib.sha256(encoded).digest())
        leader, flight = True, None
        if key is not None:
            with self.lock:
                now = self.clock()
                for expired in [k for k, row in self.cache.items() if now - row[1] >= self.ttl]:
                    del self.cache[expired]
                cached = self.cache.get(key)
                if cached is not None:
                    self.cache.move_to_end(key)
                    return self._marked(*cached, 'hit')
                flight = self.flights.get(key)
                if flight is None:
                    # Bound unique inflight bookkeeping independently of result capacity.
                    if len(self.flights) < self.capacity:
                        flight = {'event': threading.Event(), 'result': None}
                        self.flights[key] = flight
                    else:
                        key = None
                else:
                    leader = False
            if not leader:
                if not flight['event'].wait(120.0):
                    raise BudgetBusy('shared_search_wait_timeout')
                row = flight['result']
                if row is not None:
                    return self._marked(*row, 'coalesced')
                # This follower is a separate caller, not a retry of the leader.
                # Ineligible results are never reused: admit its own one workflow.
                key, flight, leader = None, None, True
        try:
            value = self.budget.run(operation)
            stamp = self.clock()
            retrieved = datetime.datetime.fromtimestamp(self.wall(), datetime.timezone.utc).isoformat()
            row = (copy.deepcopy(value), stamp, retrieved)
            eligible = self._eligible(value)
            if key is not None and eligible and len(json.dumps(value).encode()) <= 1024 * 1024:
                with self.lock:
                    self.cache[key] = row
                    while len(self.cache) > self.capacity:
                        self.cache.popitem(last=False)
            if flight is not None and leader:
                # Share only valid success; never return a failed/limited response as cached content.
                flight['result'] = row if eligible else None
            state = 'bypass_fresh' if fresh else ('miss' if key is not None else ('bypass_uncoalesced' if isinstance(scope, bytes) and scope else 'bypass_unscoped'))
            return self._marked(value, stamp, retrieved, state)
        finally:
            if key is not None and flight is not None and leader:
                with self.lock:
                    self.flights.pop(key, None)
                    flight['event'].set()


CONTROL = SearchControls()