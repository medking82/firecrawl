"""Adopt only the installed updater after an exact preimage and idle-lock check."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ROOT = Path('/home/marck/.local/state/firecrawl-updater')
# firecrawl-update is the security refresh launcher; the upgrade owner is separate.
TARGET = Path('/home/marck/.local/bin/firecrawl-upgrade')
UNITS = Path('/home/marck/.config/systemd/user')
UNIT_NAMES = ('firecrawl-upgrade-candidate.service', 'firecrawl-upgrade-candidate.timer')
REPO = Path('/home/marck/github/firecrawl')
SERVICES = ('api', 'nuq-postgres', 'playwright-service', 'redis', 'rabbitmq', 'foundationdb')
RESTART = ('services:\n' + ''.join(f'  {name}:\n    restart: unless-stopped\n' for name in SERVICES)).encode()

def install_restart_override():
    target = REPO / 'docker-compose.override.yaml'
    if any(p.is_symlink() for p in (target, REPO, *REPO.parents)):
        raise ValueError('Linked Compose boundary rejected.')
    created = False
    if target.exists() and not target.is_file():
        raise ValueError('Existing Compose override must be a regular file.')
    if not target.exists():
        if (REPO / 'docker-compose.override.yml').exists() or (REPO / 'compose.yaml').exists() or (REPO / 'compose.yml').exists():
            raise ValueError('Ambiguous Compose owner.')
        with target.open('xb') as stream: stream.write(RESTART)
        target.chmod(0o600)
        created = True
    try:
        result = subprocess.run(['docker','compose','config','--format','json'],cwd=REPO,check=True,capture_output=True,text=True,timeout=30)
        config = json.loads(result.stdout)
        if any(config['services'][name].get('restart')!='unless-stopped' for name in SERVICES):
            raise ValueError('Persistent restart settings did not render.')
    except Exception:
        if created:
            target.unlink()
        raise ValueError('Restart override validation failed; existing override preserved.') from None



def read_units(units):
    """Validate every reviewed unit before the updater or any unit changes."""
    result = []
    for unit in units:
        if unit.name not in UNIT_NAMES or unit.is_symlink() or not unit.is_file():
            raise ValueError('Unexpected unit source.')
        data = unit.read_bytes()
        if not data.startswith(b'[Unit]\n'):
            raise ValueError('Unexpected unit source.')
        target = UNITS / unit.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('Linked or unusual unit target rejected.')
        result.append((unit.name, data))
    if any(p.is_symlink() for p in (UNITS, *UNITS.parents)):
        raise ValueError('Linked unit directory rejected.')
    return result


def install_units(units):
    """Install the reviewed timer units; enabling them stays a separate Owner step."""
    result = {}
    UNITS.mkdir(parents=True, exist_ok=True)
    backups = ROOT / 'units'
    for name, data in units:
        target = UNITS / name
        if target.exists() and target.read_bytes() != data:
            backups.mkdir(mode=0o700, exist_ok=True)
            old = target.read_bytes()
            with (backups / (name + '.' + hashlib.sha256(old).hexdigest() + '.before')).open('wb') as stream: stream.write(old)
        temporary = target.with_name('.' + name + '.installing')
        with temporary.open('xb') as stream: stream.write(data)
        temporary.chmod(0o600)
        temporary.replace(target)
        result[name] = hashlib.sha256(data).hexdigest()
    return result


def install(source, expected_before, guard_source, units=()):
    # The first installation of the separate upgrade owner has no preimage.
    if expected_before != 'absent' and not re.fullmatch('[0-9a-f]{64}', expected_before):
        raise ValueError('Exact updater preimage hash (or absent) required.')
    if any(path.is_symlink() for path in (ROOT, TARGET, source, guard_source, *ROOT.parents, *TARGET.parents,
                                          *source.parents, *guard_source.parents)):
        raise ValueError('Linked updater boundary rejected.')
    present = TARGET.is_file() if expected_before != 'absent' else not TARGET.exists()
    if not ROOT.is_dir() or not present or source.is_symlink() or not source.is_file() or guard_source.is_symlink() or not guard_source.is_file():
        raise ValueError('Expected updater state and reviewed source required.')
    units = read_units(units)
    after = source.read_bytes()
    if not after.startswith(b'#!/usr/bin/env bash\n'):
        raise ValueError('Unexpected updater source.')
    matches = re.findall(rb'(?m)^guard_sha256=([a-f0-9]{64})$', after)
    guard = guard_source.read_bytes()
    if len(matches) != 1 or len(guard) > 65536 or hashlib.sha256(guard).hexdigest().encode() != matches[0]:
        raise ValueError('Reviewed standalone transport guard does not match the updater.')
    guard_digest = matches[0].decode('ascii')
    descriptor = os.open(ROOT / 'update.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError('Unsafe updater coordination lock.')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = TARGET.read_bytes() if expected_before != 'absent' else None
        if before is not None and hashlib.sha256(before).hexdigest() != expected_before:
            raise ValueError('Installed updater changed; inspect before replacing.')
        install_restart_override()
        guards = ROOT / 'guards'
        if guards.is_symlink():
            raise ValueError('Linked guard directory rejected.')
        guards.mkdir(mode=0o700, exist_ok=True)
        info = guards.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Unsafe standalone guard directory.')
        target_guard = guards / (guard_digest + '.py')
        if target_guard.is_symlink():
            raise ValueError('Linked standalone guard rejected.')
        if target_guard.exists():
            info = target_guard.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or target_guard.read_bytes() != guard):
                raise ValueError('Existing standalone guard changed.')
        else:
            with target_guard.open('xb') as stream: stream.write(guard)
            target_guard.chmod(0o600)
        receipt = ROOT / ('research-repair-' + hashlib.sha256(after).hexdigest()[:16] + '.json')
        if receipt.exists():
            # Only a version that was rolled back may be installed again; keep its old receipt.
            if json.loads(receipt.read_text()).get('status') != 'rolled_back':
                raise ValueError('Updater installation already attempted; inspect its receipt.')
            archive_receipt(receipt)
        if before is not None:
            backup = receipt.with_suffix('.before.sh')
            with backup.open('xb') as stream: stream.write(before)
            backup.chmod(0o600)
        result = {'status': 'started', 'before_sha256': expected_before, 'after_sha256': hashlib.sha256(after).hexdigest(),
                  'guard_sha256': guard_digest}
        with receipt.open('x') as stream: json.dump(result, stream)
        receipt.chmod(0o600)
        candidate = TARGET.with_name(TARGET.name + '.research-candidate')
        with candidate.open('xb') as stream: stream.write(after)
        candidate.chmod(0o700)
        if (TARGET.read_bytes() if TARGET.exists() else None) != before:
            raise ValueError('Updater changed during preparation.')
        candidate.replace(TARGET)
        if TARGET.read_bytes() != after: raise ValueError('Updater replacement verification failed.')
        result['units'] = install_units(units)
        result['status'] = 'installed'
        receipt.write_text(json.dumps(result), encoding='utf-8')
        return result
    finally:
        os.close(descriptor)


def archive_receipt(receipt):
    number = 1
    while receipt.with_name(f'{receipt.stem}.rolled-back-{number}.json').exists():
        number += 1
    backup = receipt.with_suffix('.before.sh')
    if backup.exists():
        backup.rename(receipt.with_name(f'{receipt.stem}.rolled-back-{number}.before.sh'))
    receipt.rename(receipt.with_name(f'{receipt.stem}.rolled-back-{number}.json'))


def rollback(expected_current):
    """Restore the exact preimage recorded when the running updater was installed."""
    if not re.fullmatch('[0-9a-f]{64}', expected_current):
        raise ValueError('Exact running updater hash required.')
    if any(path.is_symlink() for path in (ROOT, TARGET, *ROOT.parents, *TARGET.parents)):
        raise ValueError('Linked updater boundary rejected.')
    descriptor = os.open(ROOT / 'update.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError('Unsafe updater coordination lock.')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = TARGET.read_bytes()
        if hashlib.sha256(current).hexdigest() != expected_current:
            raise ValueError('Installed updater changed; inspect before replacing.')
        receipt = ROOT / ('research-repair-' + expected_current[:16] + '.json')
        record = json.loads(receipt.read_text()) if receipt.is_file() and not receipt.is_symlink() else {}
        if record.get('status') != 'installed' or record.get('after_sha256') != expected_current:
            raise ValueError('No completed installation receipt for the running updater.')
        backup = receipt.with_suffix('.before.sh')
        if backup.is_symlink() or not backup.is_file():
            raise ValueError('The running updater has no recorded preimage.')
        before = backup.read_bytes()
        if hashlib.sha256(before).hexdigest() != record.get('before_sha256'):
            raise ValueError('Preimage backup changed.')
        matches = re.findall(rb'(?m)^guard_sha256=([a-f0-9]{64})$', before)
        guard = ROOT / 'guards' / ((matches[0].decode('ascii') if len(matches) == 1 else 'missing') + '.py')
        if (len(matches) != 1 or guard.is_symlink() or not guard.is_file()
                or hashlib.sha256(guard.read_bytes()).hexdigest().encode() != matches[0]):
            raise ValueError('The preimage transport guard is unavailable.')
        candidate = TARGET.with_name(TARGET.name + '.rollback-candidate')
        with candidate.open('xb') as stream: stream.write(before)
        candidate.chmod(0o700)
        if TARGET.read_bytes() != current:
            candidate.unlink()
            raise ValueError('Updater changed during rollback.')
        candidate.replace(TARGET)
        if TARGET.read_bytes() != before: raise ValueError('Updater rollback verification failed.')
        record.update(status='rolled_back', restored_sha256=record['before_sha256'])
        receipt.write_text(json.dumps(record), encoding='utf-8')
        return {'status': 'rolled_back', 'from_sha256': expected_current, 'to_sha256': record['before_sha256']}
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expected-before', required=True,
                        help='hash of the running updater (or absent for a first installation)')
    parser.add_argument('--guard-source', type=Path)
    parser.add_argument('--unit', type=Path, action='append', default=[])
    parser.add_argument('--rollback', action='store_true',
                        help='restore the exact updater that the running one replaced')
    args = parser.parse_args()
    if args.rollback:
        if args.source or args.guard_source or args.unit:
            parser.error('--rollback takes only --expected-before')
        print(json.dumps(rollback(args.expected_before)))
    else:
        if not args.source or not args.guard_source:
            parser.error('--source and --guard-source are required')
        print(json.dumps(install(args.source, args.expected_before, args.guard_source, args.unit)))
