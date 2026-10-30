"""Apply the installed transport guard to a disposable image, without changing it."""
import hashlib
import os
import re
import stat
import subprocess
import sys
import types
import uuid
from pathlib import Path

image, guard_path, expected = sys.argv[1:]
if not re.fullmatch(r'sha256:[a-f0-9]{64}', image) or not re.fullmatch(r'[a-f0-9]{64}', expected):
    raise SystemExit('invalid image or guard digest')
path = Path(guard_path)
if any(p.is_symlink() for p in (path, *path.parents)):
    raise SystemExit('guard symlink rejected')
with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), 'rb') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise SystemExit('unsafe guard file')
    content = stream.read(65537)
if len(content) > 65536 or hashlib.sha256(content).hexdigest() != expected:
    raise SystemExit('guard digest mismatch')
guard = types.ModuleType('firecrawl_candidate_guard')
exec(compile(content, str(path), 'exec'), guard.__dict__)
container = subprocess.check_output(['docker', 'run', '-d', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges', '--memory', '256m', '--cpus', '1', '--pids-limit', '32',
    '--name', 'firecrawl-audit-' + uuid.uuid4().hex, '--entrypoint', 'node', '--workdir', '/app',
    '--env', 'USE_DB_AUTHENTICATION=false', '--env', 'PLAYWRIGHT_MICROSERVICE_URL=configured',
    image, '-e', 'setTimeout(()=>{},180000)'], text=True, timeout=30).strip()
try:
    if not re.fullmatch(r'[a-f0-9]{64}', container):
        raise RuntimeError('unexpected container ID')
    guard.CONTAINER = container
    result = guard.verify()
    profile = result.get('transport_profile')
    if (result.get('verified') is not True or profile not in guard.TRANSPORT_PROFILES
            or result.get('receipt_sha256') != guard.TRANSPORT_PROFILES[profile]['receipt_sha256']
            or result.get('api_image') != image or result.get('container_id') != container
            or path.read_bytes() != content):
        raise RuntimeError('transport guard rejected candidate')
    print('transport guard passed; profile=' + str(profile))
finally:
    subprocess.run(['docker', 'rm', '-f', container], check=True, stdout=subprocess.DEVNULL, timeout=30)
