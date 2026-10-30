"""Consume the advisory owner's report; local events and desktop notifications only."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

TARGETS = {'firecrawl-api-1', 'firecrawl-monitor-api-1', 'firecrawl-browser-api-1'}
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
MAX_BYTES = 8_000_000
MAX_AGE = timedelta(hours=48)


class NotifyError(Exception):
    pass


def private_dir(path):
    if os.name != 'posix':
        raise NotifyError('private storage requires POSIX ownership and permissions')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise NotifyError('storage directory must be private and owned by this user')
    return path


def read_json(path):
    # O_NOFOLLOW and fstat bind validation to the opened file, not an earlier stat.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise NotifyError('input is not a regular file')
        if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise NotifyError('input must be private and owned by this user')
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise NotifyError('input too large')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NotifyError('duplicate JSON key')
            result[key] = value
        return result

    def invalid(_):
        raise NotifyError('invalid JSON number')

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.notify-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            if os.name == 'posix':
                os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, sort_keys=True, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError('timestamp must be UTC')
    return result


def normalize(report, now):
    if not isinstance(report, dict):
        raise ValueError('invalid report')
    completed = stamp(report.get('completed_at'))
    if completed > now + timedelta(minutes=5):
        return {'status': 'error', 'reason': 'future_report'}
    if now - completed > MAX_AGE:
        return {'status': 'error', 'reason': 'stale_report'}
    status = report.get('status')
    if status == 'error':
        # Do not copy arbitrary scanner error text into events or desktop markup.
        return {'status': 'error', 'reason': 'scan_failed'}
    if status not in ('findings', 'no_high_critical_findings'):
        raise ValueError('invalid status')
    images = report.get('images')
    if not isinstance(images, list) or len(images) != len(TARGETS):
        raise ValueError('incomplete images')
    normalized, seen, count = [], set(), 0
    for image in images:
        if not isinstance(image, dict):
            raise ValueError('invalid image')
        name, identity, findings = (image.get(k) for k in ('container', 'image_id', 'findings'))
        if not isinstance(name, str) or name not in TARGETS or name in seen:
            raise ValueError('invalid container')
        seen.add(name)
        if not isinstance(identity, str) or not DIGEST.fullmatch(identity) or not isinstance(findings, list):
            raise ValueError('invalid image evidence')
        if image.get('status') != ('findings' if findings else 'no_high_critical_findings'):
            raise ValueError('inconsistent image status')
        rows = []
        for finding in findings:
            if not isinstance(finding, dict):
                raise ValueError('invalid finding')
            row = {}
            for field in ('target', 'type', 'id', 'package', 'installed_version', 'severity', 'fixed_version'):
                value = finding.get(field)
                if not isinstance(value, str) or (not value and field != 'fixed_version'):
                    raise ValueError('invalid finding field')
                row[field] = value
            if row['severity'] not in ('HIGH', 'CRITICAL'):
                raise ValueError('invalid severity')
            rows.append(row)
        count += len(rows)
        normalized.append({'container': name, 'image_id': identity,
                           'findings': sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))})
    if type(report.get('findings_count')) is not int or report['findings_count'] != count:
        raise ValueError('inconsistent count')
    if status != ('findings' if count else 'no_high_critical_findings'):
        raise ValueError('inconsistent status')
    return {'status': status, 'findings_count': count,
            'images': sorted(normalized, key=lambda image: image['container'])}


def snapshot(path, now):
    try:
        return normalize(read_json(path), now)
    except FileNotFoundError:
        return {'status': 'error', 'reason': 'missing_report'}
    except (OSError, ValueError, NotifyError, RecursionError):
        return {'status': 'error', 'reason': 'invalid_or_unreadable_report'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def desktop(value):
    status = value['status']
    if value.get('source') == 'updater':
        title = 'Firecrawl Security Refresh Failed' if status == 'error' else 'Firecrawl Security Refresh Recovered'
        body = 'Inspect The Private Local Updater Report And Event.'
    else:
        title = {'error': 'Firecrawl Advisory Scan Failed', 'findings': 'Firecrawl Advisory Findings',
                 'no_high_critical_findings': 'Firecrawl Advisory Scan Completed'}[status]
        body = ('Scan Incomplete; Inspect The Private Local Report And Event.' if status == 'error' else
                f"HIGH/CRITICAL Findings: {value['findings_count']}. Inspect The Private Local Report.")
    try:
        result = subprocess.run(['notify-send', '--app-name=Firecrawl Advisory',
                                 '--urgency=' + ('critical' if status in ('error', 'findings') else 'normal'),
                                 '--', title, body], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotifyError('notify-send unavailable or timed out') from exc
    if result.returncode:
        raise NotifyError('notify-send failed')


def load_state(path):
    try:
        value = read_json(path)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('version') != 1 or
            not isinstance(value.get('fingerprint'), str) or
            not re.fullmatch(r'[0-9a-f]{64}', value['fingerprint']) or
            not isinstance(value.get('event_id'), str) or
            not re.fullmatch(r'[0-9a-f]{32}', value['event_id']) or
            type(value.get('delivered')) is not bool or
            not isinstance(value.get('snapshot'), dict) or
            fingerprint(value['snapshot']) != value['fingerprint']):
        raise NotifyError('invalid notification state; manual inspection required')
    stamp(value.get('created_at'))
    return value


def consume(report, state_dir, send=desktop, now=None):
    """Caller must hold the state directory lock throughout this operation."""
    now = now or datetime.now(timezone.utc)
    return consume_snapshot(snapshot(Path(report), now), state_dir, send, now)


def updater_snapshot(path):
    try:
        report = read_json(Path(path))
        if not isinstance(report, dict) or report.get('status') not in ('error', 'updated', 'already-current'):
            raise ValueError('invalid updater status')
        return {'source': 'updater', **({'status': 'error', 'reason': 'updater_failed'}
                                      if report['status'] == 'error' else {'status': 'healthy'})}
    except FileNotFoundError:
        return {'source': 'updater', 'status': 'error', 'reason': 'missing_updater_report'}
    except (OSError, ValueError, NotifyError, RecursionError):
        return {'source': 'updater', 'status': 'error', 'reason': 'invalid_or_unreadable_updater_report'}


def consume_updater(report, state_dir, send=desktop, now=None):
    """Use a separate state directory, under the same caller-held lock."""
    return consume_snapshot(updater_snapshot(report), state_dir, send,
                            now or datetime.now(timezone.utc), quiet_initial=True)


def consume_snapshot(current, state_dir, send, now, quiet_initial=False):
    state_dir = private_dir(Path(state_dir))
    events = private_dir(state_dir / 'events')
    path = state_dir / 'state.json'
    previous = load_state(path)
    code = {'error': 2, 'findings': 1, 'no_high_critical_findings': 0, 'healthy': 0}[current['status']]
    if code == 2:
        print(('updater' if current.get('source') == 'updater' else 'advisory') +
              ' report error: ' + current['reason'], file=sys.stderr)
    key = fingerprint(current)
    if previous and previous['fingerprint'] == key:
        if previous['delivered']:
            return code
        state = previous
    else:
        state = {'version': 1, 'fingerprint': key, 'event_id': uuid.uuid4().hex,
                 'created_at': now.isoformat(), 'snapshot': current, 'delivered': False}
        if quiet_initial and previous is None and code == 0:
            state['delivered'] = True
            atomic_json(path, state)
            return code
        atomic_json(path, state)
    event_path = events / (state['event_id'] + '.json')
    event = {'version': 1, 'event_id': state['event_id'], 'created_at': state['created_at'],
             'fingerprint': key, 'snapshot': current, 'delivery': 'pending'}
    # An event must exist privately before any desktop side effect.
    atomic_json(event_path, event)
    try:
        if send is not None:
            send(current)
    except NotifyError:
        event['delivery'] = 'failed'
        atomic_json(event_path, event)
        print('desktop notification failed; private event retained; retry on next invocation', file=sys.stderr)
        return 2
    event['delivery'] = 'accepted' if send is not None else 'recorded'
    atomic_json(event_path, event)
    state['delivered'] = True
    atomic_json(path, state)
    return code


@contextmanager
def locked(directory):
    import fcntl
    fd = os.open(directory / 'notify.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NotifyError('lock must be private and owned by this user')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NotifyError('notification consumer already running') from exc
        yield


def main(argv=None):
    root = Path.home() / '.local/state/firecrawl-advisory'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=root / 'reports/latest.json')
    parser.add_argument('--state-dir', type=Path, default=root / 'notifications')
    parser.add_argument('--updater-report', type=Path,
                        help='also observe the optional security refresh latest.json')
    parser.add_argument('--record-only', action='store_true', help='persist deduplicated events without desktop delivery')
    args = parser.parse_args(argv)
    try:
        directory = private_dir(args.state_dir)
        with locked(directory):
            codes = []
            consumers = [(consume, args.report, directory)]
            if args.updater_report is not None:
                consumers.append((consume_updater, args.updater_report, directory / 'updater'))
            for consumer, report, state in consumers:
                try:
                    codes.append(consumer(report, state, send=None if args.record_only else desktop))
                except (OSError, ValueError, NotifyError, RecursionError):
                    print('notification consumer failed; inspect private storage and state; delivery may need retry', file=sys.stderr)
                    codes.append(2)
            return max(codes)
    except (OSError, ValueError, NotifyError, RecursionError):
        # Never print arbitrary report content, process stderr or paths.
        print('notification consumer failed; check private storage, state and lock; delivery may need retry', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
