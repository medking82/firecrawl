"""Bounded, keyless search continuity. No retries, model calls or credentials."""
import copy
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import importlib.util
from pathlib import Path
_controls_path = Path(__file__).with_name('search_controls.py')
if _controls_path.is_symlink() or not _controls_path.is_file():
    raise ImportError('Search controls sibling unavailable')
_controls_spec = importlib.util.spec_from_file_location('strata_search_controls', _controls_path)
controls = importlib.util.module_from_spec(_controls_spec)
_controls_spec.loader.exec_module(controls)

BAIDU = ('baidu.com', 'baidu.cn', 'baidu.hk')
EXA_ENDPOINT = 'https://mcp.exa.ai/mcp'
MAX_BODY = 2 * 1024 * 1024


class SearchFailure(ValueError):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def host_of(url):
    if not isinstance(url, str):
        return None
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return None
        host = urllib.parse.unquote(p.hostname).rstrip('.').encode('idna').decode().lower().rstrip('.')
        if '%' in host or '\\' in host or any(c.isspace() for c in host):
            return None
        return host
    except (ValueError, UnicodeError, TypeError):
        return None


def blocked_host(host):
    return bool(host) and any(host == d or host.endswith('.' + d) for d in BAIDU)


def allowed_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return False
    except (ValueError, TypeError):
        return False
    host = host_of(url)
    return bool(host) and not blocked_host(host)


def reject_baidu_targets(value, key=''):
    """Reject explicit page targets; never execute a query or parse its prose as URLs."""
    if isinstance(value, dict):
        for k, v in value.items():
            reject_baidu_targets(v, k.lower())
    elif isinstance(value, list):
        for v in value:
            reject_baidu_targets(v, key)
    elif isinstance(value, str) and (key in ('url', 'urls', 'targeturl', 'baseurl', 'originurl')):
        if '\\' in value or any(ord(c) < 32 for c in value):
            raise SearchFailure('ambiguous_page_url', 400)
        candidate = value if '://' in value else 'https://' + value.lstrip('/')
        try:
            parsed = urllib.parse.urlsplit(candidate)
            if parsed.username is not None or parsed.password is not None:
                raise SearchFailure('page_url_credentials_not_supported', 400)
        except ValueError:
            raise SearchFailure('invalid_page_url', 400) from None
        if blocked_host(host_of(candidate)):
            raise SearchFailure('baidu_page_excluded', 400)
    elif isinstance(value, str) and key in ('engine', 'engines', 'provider', 'providers'):
        if re.search(r'\bbaidu\b', value, re.I):
            raise SearchFailure('baidu_engine_excluded', 400)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def exa_search(query, limit):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'web_search_exa', 'arguments': {'query': query, 'numResults': limit,
        'objective': 'Return direct source URLs and evidence. Exclude baidu.com, baidu.cn, baidu.hk and their subdomains.'}}}
    req = urllib.request.Request(EXA_ENDPOINT, data=json.dumps(body).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                 'MCP-Protocol-Version': '2025-06-18', 'User-Agent': 'strata-public-web/1.0'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            raw = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        raise SearchFailure('exa_rate_limited' if e.code == 429 else 'exa_http_error',
                            429 if e.code == 429 else 503) from None
    except (OSError, ValueError):
        raise SearchFailure('exa_unavailable') from None
    if len(raw) > MAX_BODY:
        raise SearchFailure('exa_response_too_large')
    return parse_exa(raw, limit)


def parse_exa(raw, limit):
    try:
        text = raw.decode('utf-8')
        messages = [json.loads(text)] if text.lstrip().startswith('{') else [
            json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith('data:')]
        reply = next((v for v in messages if isinstance(v, dict) and v.get('id') == 1), {})
        result = reply.get('result', {})
        # Exa may signal the anonymous quota without MCP isError or HTTP429.
        meta = result.get('_meta') if isinstance(result, dict) else None
        if isinstance(meta, dict) and meta.get('ai.exa/rateLimited') is True:
            raise SearchFailure('exa_rate_limited', 429)
        if reply.get('error') or (isinstance(result, dict) and result.get('isError')):
            error_text = json.dumps(reply.get('error') or result.get('content', []))
            if re.search(r'rate[ -]?limit|too many requests|\b429\b', error_text, re.I):
                raise SearchFailure('exa_rate_limited', 429)
            raise SearchFailure('exa_service_error')
        if not isinstance(result, dict):
            raise ValueError()
        content = result.get('content')
        if not isinstance(content, list) or not content:
            raise ValueError()
        items, recognized = [], False
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str):
                continue
            value = block['text'].strip()
            if re.fullmatch(r'No (?:search )?results(?: found)?[.!]?', value, re.I):
                recognized = True
            for m in re.finditer(r'^Title: ([^\n]*)\nURL: (https?://[^\s]+)\n(.*?)(?=\n---\n\nTitle: |\Z)', value, re.M | re.S):
                recognized = True
                title, url, evidence = m.groups()
                if len(url) <= 2048 and allowed_url(url):
                    items.append({'title': title[:2000], 'url': url, 'description': evidence[:2000]})
        if not recognized:
            raise ValueError()
        return items[:limit]
    except SearchFailure:
        raise
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise SearchFailure('exa_invalid_response') from None


class FallbackGate:
    """Bound inflight anonymous calls; honor 429 locally instead of retrying."""
    def __init__(self):
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.cooldown_until = 0.0

    def call(self, fn, query, limit):
        with self.lock:
            if time.monotonic() < self.cooldown_until:
                raise SearchFailure('exa_rate_limited', 429)
        if not self.slots.acquire(blocking=False):
            raise SearchFailure('exa_busy', 503)
        try:
            return fn(query, limit)
        except SearchFailure as e:
            if e.status == 429:
                with self.lock:
                    self.cooldown_until = time.monotonic() + 60
            raise
        finally:
            self.slots.release()


FALLBACK_GATE = FallbackGate()


def fallback_compatible(body):
    # Exa's current keyless tool cannot guarantee Firecrawl advanced constraints.
    # The backend still receives the full request; only fallback is restricted.
    allowed = {'query', 'limit', 'sources', 'excludeDomains', 'includeDomains', 'timeout',
               'origin', 'integration', 'clientModel', 'toolDetail', 'domainTools', 'highlights', 'ignoreInvalidURLs'}
    if body.get('highlights') is True or body.get('ignoreInvalidURLs') is True:
        return False
    if set(body) - allowed:
        return False
    sources = body.get('sources', ['web'])
    if not isinstance(sources, list):
        return False
    types = [x.get('type') if isinstance(x, dict) and set(x) == {'type'} else x for x in sources]
    return 'web' in types and all(x in ('web', 'alexandria') for x in types)


def domain_matches(url, domains):
    host = host_of(url)
    return any(host == d or (host and host.endswith('.' + d)) for d in domains)


def search(body, primary, fallback=exa_search, gate=FALLBACK_GATE):
    """Return Firecrawl v2 envelope with additive provenance; callbacks are bounded."""
    if not isinstance(body, dict) or not isinstance(body.get('query'), str) or not body['query'].strip():
        raise SearchFailure('invalid_search_query', 400)
    query, limit = body['query'], body.get('limit', 5)
    if len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 100:
        raise SearchFailure('invalid_search_limit_or_query', 400)
    reject_baidu_targets(body)
    request = copy.deepcopy(body)
    for key in ('includeDomains', 'excludeDomains'):
        domains = request.get(key, [])
        if not isinstance(domains, list) or any(not isinstance(x, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', x) for x in domains):
            raise SearchFailure('invalid_domain_filter', 400)
        request[key] = [x.lower().rstrip('.') for x in domains]
    if any(blocked_host(d) for d in request['includeDomains']):
        raise SearchFailure('baidu_page_excluded', 400)
    request['excludeDomains'] = list(dict.fromkeys(request['excludeDomains'] + list(BAIDU)))
    if not request['includeDomains']:
        request.pop('includeDomains')
    envelope, reason, explicit_empty = None, 'primary_unavailable', False
    try:
        envelope = primary(request)
    except SearchFailure as e:
        if e.status < 500 and e.status != 429:
            raise
        reason = e.code
    except (OSError, ValueError):
        pass
    if isinstance(envelope, dict) and envelope.get('success') is True and isinstance(envelope.get('data'), dict):
        envelope = copy.deepcopy(envelope)
        data = envelope['data']
        count = 0
        for key in ('web', 'news', 'images'):
            if key not in data:
                continue
            if not isinstance(data[key], list):
                raise SearchFailure('primary_invalid_response')
            rows = data[key]
            data[key] = [r for r in rows if isinstance(r, dict) and allowed_url(r.get('url'))
                         and not any(blocked_host(host_of(r.get(k))) for k in ('imageUrl', 'sourceUrl'))]
            count += len(data[key])
        explicit_empty = isinstance(data.get('web'), list) and not data['web'] and not any(envelope['data'].get(k) for k in ('news', 'images'))
        # A concrete web=[] has zero-result semantics; {} remains ambiguous upstream.
        reason = 'primary_no_allowed_results' if explicit_empty else 'primary_empty_unknown'
        if count:
            envelope['searchContinuity'] = {'provider': 'firecrawl', 'fallback': False}
            return envelope
    elif isinstance(envelope, dict) and envelope.get('success') is False:
        reason = 'primary_error'
    if not fallback_compatible(body):
        if isinstance(envelope, dict) and envelope.get('success') is True:
            envelope['searchContinuity'] = {'provider': 'firecrawl', 'fallback': False,
                'warning': reason, 'fallbackSkipped': 'unsupported_search_constraints'}
            return envelope
        raise SearchFailure('primary_failed_fallback_constraints')
    try:
        rows = gate.call(fallback, query, min(limit, 5))
        if not isinstance(rows, list):
            raise SearchFailure('exa_invalid_response')
        rows = [r for r in rows if isinstance(r, dict) and allowed_url(r.get('url'))
                and not domain_matches(r['url'], request['excludeDomains'])
                and (not request.get('includeDomains') or domain_matches(r['url'], request['includeDomains']))]
    except (OSError, ValueError) as e:
        code = e.code if isinstance(e, SearchFailure) else 'exa_unavailable'
        if explicit_empty:
            envelope['searchContinuity'] = {'provider': 'firecrawl', 'fallback': False,
                'warning': reason, 'fallbackError': code}
            return envelope
        raise SearchFailure('search_unavailable_' + code, getattr(e, 'status', 503)) from None
    # Official SDKs discard unknown envelope fields. Keep provenance in the
    # existing description field too; never invent a Firecrawl feedback id/credit.
    sources = body.get('sources', ['web'])
    partial = body.get('domainTools') is True or 'alexandria' in [x.get('type') if isinstance(x, dict) else x for x in sources]
    note = '; web fallback only, tool discovery not supplied by Exa' if partial else ''
    rows = [{**r, 'description': '[Search provider: Exa; Firecrawl status: ' + reason + note + '] ' + str(r.get('description') or '')} for r in rows]
    data = copy.deepcopy(envelope['data']) if isinstance(envelope, dict) and envelope.get('success') is True and isinstance(envelope.get('data'), dict) else {}
    data['web'] = rows[:limit]
    return {'success': True, 'data': data, 'searchContinuity': {
        'provider': 'exa', 'fallback': True, 'primaryStatus': reason,
        'fallbackCapabilities': ['web'], 'toolDiscoveryFallback': False,
        'partialCapabilities': partial, 'resultLimit': min(limit, 5), 'billing': 'existing_anonymous_rate_limited_route'}}
