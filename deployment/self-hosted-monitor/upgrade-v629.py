"""Monitor-only v2.11.629 upgrade owner; execute only after review and preflight."""
import argparse
import ast
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / 'patched-base'))
import promote as p
from recovery_backup import verify_backup
from upgrade_state import stable_summary
from upgrade_stop_candidate import stop_candidate

PROJECT = 'firecrawl-monitor'
EXPECTED = 'sha256:89b2bacf0c5f6d9a183d8f9f5ff9ff5ae64a1757b61fd5509990aa2f804398f4'
BASE_IMAGE = 'sha256:b967b93629d2a1c2a46ab160058f29f5d23ae67e6e8b3880656ec4d0f8d78cfd'
CURRENT_IMAGE = 'sha256:079ea46137172ec51f50d882beef02b0a7cd55408c9d87624f94f3c730ae94c3'
CURRENT_BASE_REF = 'sha256:f4495407eea50a90846f4571f407253948e9be2eabfcc988f658bca1096417ff'
GUARD_SHA = '07f73956c9531c483ccd9816af9b3aa06de87424dc0ab33758e9daa2c5e16c3d'
UPDATER_SHA = 'b853b7a642a24238b0f9aa540263a9343d87e490e5e609a7271f29b3f918a664'
TRANSPORT_SCRIPT_SHA = '558cb5d6354dcdbf7394e70d0f8130e9e43ee5c0fb312cf4b55dd6504f3d4f69'
ENV = p.MONITOR / 'runtime.env'
MIGRATION = BASE / '003-agent-sponsors.sql'


def sql(container, db, query):
    return p.run(['docker', 'exec', container, 'psql', '-X', '-U', 'postgres', '-d', db,
                  '-v', 'ON_ERROR_STOP=1', '-At', '-c', query], timeout=30)


def idle(db):
    queue = "SELECT (SELECT count(*) FROM nuq.queue_scrape WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_crawl_finished WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_scrape_backlog)"
    checks = "SELECT count(*) FROM public.monitor_checks WHERE status IN ('queued','running')"
    activity = "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state='active'"
    return sql(db, 'postgres', queue) == '0' and sql(db, 'monitor', checks) == '0' and sql(db, 'postgres', activity) == '0'


def check_schema(db):
    # 629 has no schema delta; verify the existing auth projection without DDL.
    sql(db, 'monitor', 'SELECT status, verification_deadline, email FROM public.agent_sponsors WHERE api_key_id = -1 LIMIT 1')


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_image_binding(current, base, candidate):
    if current['Image'] != CURRENT_IMAGE or base['Image'] != BASE_IMAGE:
        raise p.Stop('current monitor or base image changed')
    parent = p.inspect('image', BASE_IMAGE).get('RootFS', {}).get('Layers')
    layers = p.inspect('image', candidate).get('RootFS', {}).get('Layers')
    if not isinstance(parent, list) or not parent or not isinstance(layers, list) or len(layers) <= len(parent) or layers[:len(parent)] != parent:
        raise p.Stop('candidate image does not extend the reviewed base layers')


def env_map(config):
    pairs = config.get('Env') or []
    result = {}
    for item in pairs:
        key, sep, value = item.partition('=')
        if not sep or key in result: raise p.Stop('invalid image environment')
        result[key] = value
    return result


def verify_image_configuration():
    old_image = p.inspect('image', CURRENT_IMAGE)['Config']
    new_image = p.inspect('image', EXPECTED)['Config']
    old_env, new_env = env_map(old_image), env_map(new_image)
    env_changes = {k for k in old_env.keys() | new_env.keys() if old_env.get(k) != new_env.get(k)}
    if env_changes != {'FIRECRAWL_BUILD_SHA'} or old_env['FIRECRAWL_BUILD_SHA'] != 'cccc2a6e554be566e1a504def286fca281b69024' or new_env['FIRECRAWL_BUILD_SHA'] != '74f81da6935bcbf581480a888c2ed3fd81fc2d85':
        raise p.Stop('unreviewed image environment difference')
    old_labels, new_labels = old_image.get('Labels') or {}, new_image.get('Labels') or {}
    label_changes = {k for k in old_labels.keys() | new_labels.keys() if old_labels.get(k) != new_labels.get(k)}
    allowed = set()
    if label_changes != allowed or any(k in new_labels for k in allowed):
        raise p.Stop('unreviewed image label difference')
    return old_image, new_image


def verify_api_configuration(before, after):
    old_image, new_image = verify_image_configuration()
    old_env, new_env = env_map(old_image), env_map(new_image)
    old_labels, new_labels = old_image.get('Labels') or {}, new_image.get('Labels') or {}
    left, right = p.api_configuration(before), p.api_configuration(after)
    before_env, after_env = env_map(left['config']), env_map(right['config'])
    if before_env.get('FIRECRAWL_BUILD_SHA') != old_env['FIRECRAWL_BUILD_SHA'] or after_env.get('FIRECRAWL_BUILD_SHA') != new_env['FIRECRAWL_BUILD_SHA']:
        raise p.Stop('container build SHA differs from reviewed image')
    left['config']['Env'] = [x for x in left['config']['Env'] if not x.startswith('FIRECRAWL_BUILD_SHA=')]
    right['config']['Env'] = [x for x in right['config']['Env'] if not x.startswith('FIRECRAWL_BUILD_SHA=')]
    for key in ('org.firecrawl.patch.base', 'org.firecrawl.patch.base-image-id'):
        if (before['Config'].get('Labels') or {}).get(key) != old_labels.get(key) or (after['Config'].get('Labels') or {}).get(key) != new_labels.get(key):
            raise p.Stop('container image label differs from reviewed image')
    if left != right: raise p.Stop('monitor API configuration changed')


def verify_current_image_configuration(before, old_image):
    expected = env_map(old_image)
    actual = env_map(before['Config'])
    if actual.get('FIRECRAWL_BUILD_SHA') != expected.get('FIRECRAWL_BUILD_SHA'):
        raise p.Stop('current container build SHA differs from image')
    labels = old_image.get('Labels') or {}
    for key in ('org.firecrawl.patch.base', 'org.firecrawl.patch.base-image-id'):
        if (before['Config'].get('Labels') or {}).get(key) != labels.get(key):
            raise p.Stop('current container image label differs from image')


def installed_package_audit():
    updater = Path('/home/marck/.local/bin/firecrawl-update')
    raw = updater.read_bytes()
    if hashlib.sha256(raw).hexdigest() != UPDATER_SHA:
        raise p.Stop('installed updater identity changed')
    source = raw.decode('utf-8')
    blocks = source.split("<<'PYCANDIDATE'\n", 1)
    if len(blocks) != 2: raise p.Stop('installed package audit not found')
    tree = ast.parse(blocks[1].split('\nPYCANDIDATE', 1)[0])
    values = [node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'package_audit_source' for t in node.targets)
              and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(values) != 1: raise p.Stop('installed package audit is ambiguous')
    result = p.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--memory', '256m',
                    '--cpus', '1', '--pids-limit', '32', '--user', '0', '--entrypoint', 'node',
                    EXPECTED, '-e', values[0], '/'], timeout=240)
    packages = json.loads(result)
    if set(packages) != {'decode-uri-component', 'systeminformation'} or any(not x for x in packages.values()):
        raise p.Stop('installed package audit returned invalid evidence')
    return packages


def http_idle():
    return not p.run(['ss', '-H', '-tn', 'state', 'established', '( sport = :3006 )'], timeout=10)


def wait_ready():
    end = time.monotonic() + 180
    while time.monotonic() < end:
        try:
            if p.readiness(3006): return
        except Exception: pass
        time.sleep(2)
    raise p.Stop('monitor readiness timeout')


def upgrade(backup, max_age, guard, guard_sha):
    os.umask(0o077)
    with p.locks():
        backup_evidence = verify_backup(backup, max_age)
        snapshot = p.project_snapshot(PROJECT)
        if snapshot.get('envfile') and Path(snapshot['envfile']) != ENV:
            raise p.Stop('unexpected monitor environment owner')
        current = snapshot['services']['api']; database = snapshot['services']['app-db']
        if not database['State']['Running']:
            raise p.Stop('monitor database is not running')
        other_snapshots = {name: p.project_snapshot(name) for name in ('firecrawl', 'firecrawl-browser')}
        before_others = {name: p.summary(other_snapshots[name]) for name in other_snapshots}
        stable_summary(p.summary(snapshot))
        for summary in before_others.values(): stable_summary(summary)
        verify_image_binding(current, other_snapshots['firecrawl']['services']['api'], EXPECTED)
        old_image, _ = verify_image_configuration()
        verify_current_image_configuration(current, old_image)
        env_bytes = ENV.read_bytes(); env_stat = ENV.stat()
        if hasattr(os, 'listxattr') and os.listxattr(ENV):
            raise p.Stop('runtime environment has unsupported extended attributes')
        if p.inspect('image', p.env_value(env_bytes, 'MONITOR_IMAGE'))['Id'] != current['Image']:
            raise p.Stop('monitor image reference disagrees with running API')
        if p.env_value(env_bytes, 'BASE_API') != CURRENT_BASE_REF:
            raise p.Stop('monitor base reference changed')
        if p.inspect('image', EXPECTED)['Id'] != EXPECTED:
            raise p.Stop('reviewed candidate image unavailable')
        config = p.render(snapshot, envfile=ENV); expected_config = config_hash(config)
        updated = p.replace_env(env_bytes, {'MONITOR_IMAGE': EXPECTED, 'BASE_API': BASE_IMAGE})
        with tempfile.TemporaryDirectory(dir=p.UPDATER) as temp:
            staged_env = Path(temp) / 'runtime.env'
            staged_env.write_bytes(updated); staged_env.chmod(0o600)
            proposed = p.render(snapshot, envfile=staged_env)
            if p.without_images(proposed) != p.without_images(config):
                raise p.Stop('candidate changes non-image Compose configuration')
            for service, settings in config['services'].items():
                target = proposed['services'][service].get('image')
                expected = EXPECTED if service == 'api' else settings.get('image')
                if target != expected:
                    raise p.Stop('candidate changes another service image')
        files = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in snapshot['paths']}
        migration = MIGRATION.read_text(encoding='utf-8'); migration_sha = hashlib.sha256(migration.encode()).hexdigest()
        # Existing installed guard is the admission authority, never an inferred candidate profile.
        if guard_sha != GUARD_SHA or Path(guard) != p.UPDATER / 'guards' / (GUARD_SHA + '.py'):
            raise p.Stop('unexpected installed transport guard')
        transport_script = BASE.parent / 'patched-base' / 'verify-transport.py'
        if hashlib.sha256(transport_script.read_bytes()).hexdigest() != TRANSPORT_SCRIPT_SHA:
            raise p.Stop('packaged transport verifier differs from reviewed source')
        p.run([sys.executable, '-B', str(transport_script), EXPECTED, guard, guard_sha], timeout=240)
        packages = installed_package_audit()
        receipt_dir = p.UPDATER / ('monitor-v629-' + uuid.uuid4().hex)
        receipt_dir.mkdir(mode=0o700)
        p.atomic_write(receipt_dir / 'runtime.env.before', env_bytes)
        record = {'status': 'preflight', 'backup': backup_evidence, 'image_before': current['Image'],
                  'candidate': EXPECTED, 'api_before': current['Id'], 'db_id': database['Id'],
                  'compose_sha256': files, 'config_sha256': expected_config,
                  'env_sha256': hashlib.sha256(env_bytes).hexdigest(), 'migration_sha256': migration_sha,
                  'transport_guard_sha256': guard_sha, 'before': p.summary(snapshot)}
        record['package_check'] = packages
        def save(): p.atomic_write(receipt_dir / 'receipt.json', json.dumps(record, indent=2).encode())
        save()
        candidate_attempted = False
        try:
            deadline = time.monotonic() + 120
            consecutive = 0
            while consecutive < 2:
                if idle(database['Id']) and http_idle(): consecutive += 1
                else: consecutive = 0
                if time.monotonic() >= deadline: raise p.Stop('monitor did not drain')
                time.sleep(2)
            if (stable_summary(p.summary(p.project_snapshot(PROJECT))) != stable_summary(record['before']) or ENV.read_bytes() != env_bytes
                or any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != value for path,value in files.items())
                or config_hash(p.render(snapshot, envfile=ENV)) != expected_config):
                raise p.Stop('monitor state changed during preflight')
            record['status'] = 'stopping-api'; save()
            p.run(['docker', 'stop', '--time', '60', current['Id']], timeout=90)
            try:
                quiet = not p.inspect('container', current['Id'])['State']['Running'] and idle(database['Id'])
            except Exception:
                quiet = False
            if not quiet:
                p.run(['docker', 'start', current['Id']], timeout=90)
                wait_ready()
                raise p.Stop('monitor busy or unverified; original API restarted')
            if ENV.read_bytes() != env_bytes or ENV.stat() != env_stat or MIGRATION.read_text(encoding='utf-8') != migration:
                raise p.Stop('environment or migration changed after quiescence')
            record['status'] = 'checking-schema'; save()
            check_schema(database['Id'])
            record['status'] = 'updating-image-reference'; save()
            p.atomic_write(ENV, updated, env_stat.st_mode & 0o777, (env_stat.st_uid, env_stat.st_gid))
            if p.without_images(p.render(snapshot, envfile=ENV)) != p.without_images(config):
                raise p.Stop('unexpected Compose configuration change')
            record['status'] = 'recreating-monitor-api'; save()
            candidate_attempted = True
            p.compose(PROJECT, snapshot['paths'], snapshot['workdir'], ENV, 'up', '-d', '--no-deps',
                      '--no-build', '--pull', 'never', '--force-recreate', 'api')
            after = p.project_snapshot(PROJECT)
            if after['services']['api']['Image'] != EXPECTED:
                raise p.Stop('monitor image mismatch')
            before_nonapi = p.summary(snapshot); before_nonapi.pop('api')
            after_nonapi = p.summary(after); after_nonapi.pop('api')
            if stable_summary(before_nonapi) != stable_summary(after_nonapi):
                raise p.Stop('monitor non-API services changed')
            verify_api_configuration(current, after['services']['api'])
            wait_ready()
            for name in before_others:
                if stable_summary(p.summary(p.project_snapshot(name))) != stable_summary(before_others[name]):
                    raise p.Stop('other project changed during monitor upgrade')
            record['after'] = p.summary(after); record['status'] = 'ready'; save()
            return receipt_dir / 'receipt.json'
        except Exception as exc:
            record['failure_stage'] = record['status']; record['status'] = 'failed-manual-intervention'
            record['failure'] = str(exc) if isinstance(exc, p.Stop) else type(exc).__name__
            if candidate_attempted:
                try:
                    record['candidate_cleanup'] = stop_candidate(p, EXPECTED)
                except Exception as cleanup_exc:
                    record['candidate_cleanup'] = 'failed'
                    record['candidate_cleanup_error'] = str(cleanup_exc) if isinstance(cleanup_exc, p.Stop) else type(cleanup_exc).__name__
            save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True); parser.add_argument('--max-backup-age-seconds', required=True, type=int)
    parser.add_argument('--guard', required=True); parser.add_argument('--guard-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps({'status': 'ready', 'receipt': str(upgrade(args.backup, args.max_backup_age_seconds, args.guard, args.guard_sha256))}))


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(p.Stop('upgrade interrupted')))
    try: main()
    except Exception as exc:
        print('monitor upgrade stopped: ' + (str(exc) if isinstance(exc, p.Stop) else type(exc).__name__) + '; retain evidence', file=sys.stderr)
        sys.exit(1)
