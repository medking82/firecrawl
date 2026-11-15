"""Frozen reviewed search runtime; embedded into the standalone updater.

Changing executable mounts or isolation requires source review and requalification.
Image IDs are deployment inputs and are verified separately, never learned here.
"""
import hashlib
import json
from pathlib import Path
import re

EXPECTED = {'services': {'search-worker': {'restart': 'unless-stopped', 'cap_drop': ['ALL'], 'cpus': 1, 'command': ['--require', '/app/searxng-timeout.cjs', '/app/search-worker.cjs'], 'dns': ['127.0.0.1'], 'entrypoint': ['node'], 'environment': {'SEARCH_TRACE': '1', 'SEARCH_WORKER_ROLE': '1', 'SEARXNG_CATEGORIES': 'general', 'SEARXNG_ENDPOINT': 'http://searxng:8080', 'SEARXNG_ENGINES': 'brave,yahoo', 'USE_DB_AUTHENTICATION': 'false'}, 'mem_limit': '536870912', 'networks': {'search': {'ipv4_address': '172.30.247.20'}}, 'read_only': True, 'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp:rw,noexec,nosuid,size=32m'], 'user': '1000:1000', 'volumes': [{'type': 'bind', 'source': '/opt/firecrawl-search/searxng-timeout.cjs', 'target': '/app/searxng-timeout.cjs', 'read_only': True, 'bind': {}}, {'type': 'bind', 'source': '/opt/firecrawl-search/search-deadline.cjs', 'target': '/app/search-deadline.cjs', 'read_only': True, 'bind': {}}, {'type': 'bind', 'source': '/opt/firecrawl-search/search-worker.cjs', 'target': '/app/search-worker.cjs', 'read_only': True, 'bind': {}}], 'working_dir': '/app', 'expose': ['8081']}, 'search-egress': {'restart': 'unless-stopped', 'cap_drop': ['ALL'], 'cpus': 1, 'command': [], 'entrypoint': ['python3', '-I', '-B', '/app/search_egress.py'], 'mem_limit': '134217728', 'networks': {'search': {'ipv4_address': '172.30.247.40'}, 'search_egress': {'gw_priority': 1, 'ipv4_address': '172.30.248.2'}}, 'read_only': True, 'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp:rw,noexec,nosuid,size=32m'], 'user': '65534:65534', 'volumes': [{'type': 'bind', 'source': '/opt/firecrawl-search/search_egress.py', 'target': '/app/search_egress.py', 'read_only': True, 'bind': {}}], 'working_dir': '/app', 'expose': ['3128']}, 'searxng': {'restart': 'unless-stopped', 'cap_drop': ['ALL'], 'cpus': 1, 'command': None, 'dns': ['127.0.0.1'], 'entrypoint': None, 'environment': {'SEARXNG_SECRET': '<private>'}, 'mem_limit': '536870912', 'networks': {'search': {'ipv4_address': '172.30.247.30'}}, 'security_opt': ['no-new-privileges:true'], 'volumes': [{'type': 'bind', 'source': '/opt/firecrawl-search/settings.yml', 'target': '/etc/searxng/settings.yml', 'read_only': True, 'bind': {}}], 'expose': ['8080'], 'healthcheck': {'test': ['CMD', 'wget', '-q', '-O', '/dev/null', 'http://127.0.0.1:8080/healthz'], 'timeout': '5s', 'interval': '30s', 'retries': 3}}}, 'networks': {'search': {'name': 'firecrawl_search', 'driver': 'bridge', 'driver_opts': {'com.docker.network.bridge.name': 'br-fc-search'}, 'ipam': {'config': [{'subnet': '172.30.247.0/24'}]}, 'internal': True, 'enable_ipv6': False}, 'search_egress': {'name': 'firecrawl_search_egress', 'driver': 'bridge', 'driver_opts': {'com.docker.network.bridge.name': 'br-fc-egress'}, 'ipam': {'config': [{'subnet': '172.30.248.0/24'}]}, 'enable_ipv6': False}}, 'api': {'volumes': [{'type': 'volume', 'source': 'fdb-cluster-file', 'target': '/var/fdb', 'read_only': True, 'volume': {}}, {'type': 'bind', 'source': '/opt/firecrawl-search/searxng-timeout.cjs', 'target': '/app/searxng-timeout.cjs', 'read_only': True, 'bind': {}}, {'type': 'bind', 'source': '/opt/firecrawl-search/search-deadline.cjs', 'target': '/app/search-deadline.cjs', 'read_only': True, 'bind': {}}], 'networks': {'backend': None, 'search': {'ipv4_address': '172.30.247.10'}}, 'environment': {'NODE_OPTIONS': '--require=/app/searxng-timeout.cjs', 'SEARCH_TRACE': '1', 'SEARCH_WORKER_URL': 'http://search-worker:8081/v2/search', 'SEARXNG_CATEGORIES': 'general', 'SEARXNG_ENDPOINT': 'http://searxng:8080', 'SEARXNG_ENGINES': 'brave,yahoo'}}, 'searx_secret_keys': ['SEARXNG_SECRET'], 'files': {'/opt/firecrawl-search/isolation.nft': '6aec975f4633df6ac9c37c5d57ecb2a77ca723052351248476dd9c8c45c7878e', '/opt/firecrawl-search/searxng-timeout.cjs': 'cd817b3dc902cab028d931b8eef49c6b0b806c3886773a42b37b54fba41516e0', '/opt/firecrawl-search/search_egress.py': '76cbe7ddf85c92d241df6c3e5864d5c8a7e1cade9137e15ef4b13a257763d9cb', '/opt/firecrawl-search/settings.yml': '25b2bf490f0db087a9df14e77da1d09c8f17b8ae0982ac50726d164bb4a41179', '/opt/firecrawl-search/search-worker.cjs': '7cfa7550402f39a20a3dcbee2aae1c72033d4185dd06bd995a212acfd40f4da6', '/opt/firecrawl-search/search-deadline.cjs': 'b10e175c301d990c177ebd67794dea2face560bf3dd5fa208a8970e7ce37705a'}}


def search_contract(config, check_files=True):
    services = config['services']
    names = set(EXPECTED['services'])
    api = services['api']
    env = api.get('environment') or {}
    active = bool(names & set(services) or env.get('NODE_OPTIONS') or env.get('SEARCH_WORKER_URL'))
    if not active:
        return None
    if not names <= set(services):
        raise ValueError('search_service_missing')
    for name, expected in EXPECTED['services'].items():
        value = json.loads(json.dumps(services[name]))
        for key in ('image','pull_policy'): value.pop(key, None)
        if name == 'searxng':
            private = value.get('environment') or {}
            if sorted(private) != EXPECTED['searx_secret_keys'] or any(not v for v in private.values()):
                raise ValueError('search_secret_contract')
            value['environment'] = {k:'<private>' for k in private}
        if value != expected:
            raise ValueError('search_service_drift')
    for name, expected in EXPECTED['networks'].items():
        if config.get('networks',{}).get(name) != expected:
            raise ValueError('search_network_drift')
    for key in ('volumes','networks'):
        if api.get(key) != EXPECTED['api'][key]:
            raise ValueError('search_api_drift')
    selected = {k:v for k,v in env.items() if k.startswith(('SEARCH_','SEARXNG_')) or k=='NODE_OPTIONS'}
    if selected != EXPECTED['api']['environment']:
        raise ValueError('search_environment_drift')
    if check_files:
        for name, expected in EXPECTED['files'].items():
            p = Path(name)
            if any(v.is_symlink() for v in (p,*p.parents)) or not p.is_file():
                raise ValueError('search_file_unavailable')
            if hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                raise ValueError('search_file_drift')
    return EXPECTED['files'].copy()


def search_image_overlay(path):
    # Exactly one historical API pin. It grants no permission for other overlays.
    path = Path(path)
    if path != Path.cwd()/'docker-compose.search-image.json': return False
    if any(v.is_symlink() for v in (path,*path.parents)): return False
    value = json.loads(path.read_bytes())
    if set(value) != {'services'} or set(value['services']) != {'api'}: return False
    entry = value['services']['api']
    return (set(entry) <= {'image','pull_policy'} and entry.get('pull_policy','never') == 'never'
            and re.fullmatch(r'sha256:[a-f0-9]{64}',entry.get('image','')) is not None)


def verify_live_transport(guard, config):
    """Check the frozen preload separately; transport probes run without it."""
    import subprocess
    if not search_contract(config):
        return guard.verify()
    def inspect(identity):
        return json.loads(subprocess.check_output(['docker','inspect',identity],timeout=10))[0]
    before = inspect(guard.CONTAINER)
    identity = before['Id']
    if not re.fullmatch(r'[a-f0-9]{64}',identity) or before['State']['Running'] is not True:
        raise ValueError('search_container_identity')
    env = dict(v.split('=',1) for v in before['Config']['Env'] if '=' in v)
    selected = {k:v for k,v in env.items() if k.startswith(('SEARCH_','SEARXNG_')) or k=='NODE_OPTIONS'}
    if selected != EXPECTED['api']['environment']:
        raise ValueError('search_live_environment_drift')
    mounts = before['Mounts']
    expected = EXPECTED['api']['volumes']
    if len(mounts) != len(expected):
        raise ValueError('search_live_mount_drift')
    for item in expected:
        matches = [v for v in mounts if v['Destination']==item['target'] and v['Type']==item['type'] and v['RW'] is False]
        if len(matches)!=1:
            raise ValueError('search_live_mount_drift')
        if item['type']=='bind' and matches[0]['Source']!=item['source']:
            raise ValueError('search_live_mount_drift')
        if item['type']=='volume' and matches[0].get('Name')!='firecrawl_'+item['source']:
            raise ValueError('search_live_mount_drift')
    original, original_container = guard.run_json, guard.CONTAINER
    def sanitized(arguments, stage, deadline):
        # Pin every exec to the inspected container, and never start Node with
        # inherited preload code before its transport probe can run.
        if arguments[:2]==['docker','exec']:
            if arguments[2:4]!=[identity,'node']:
                raise ValueError('search_probe_identity')
            arguments=arguments[:2]+['-e','NODE_OPTIONS=']+arguments[2:]
        return original(arguments,stage,deadline)
    try:
        guard.CONTAINER=identity
        guard.run_json=sanitized
        result=guard.verify()
        after=inspect(identity)
        if any(after[k]!=before[k] for k in ('Id','Image','Config','Mounts')) or after['State']['Running'] is not True:
            raise ValueError('search_live_changed')
        search_contract(config)
        return result
    finally:
        guard.run_json,guard.CONTAINER=original,original_container
