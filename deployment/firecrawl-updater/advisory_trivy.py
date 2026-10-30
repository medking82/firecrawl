"""Standalone, read-only image advisory scan; no updater integration."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tarfile
from datetime import datetime, timezone

TRIVY = 'sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c'
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
SEVERITIES = {'UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}


class ScanError(Exception):
    pass


def run(args):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScanError('command unavailable or timed out') from exc
    if result.returncode != 0:
        # Never return Docker/Trivy stderr: it can contain credentials or image data.
        raise ScanError('command failed: ' + ' '.join(args[:3]))
    if len(result.stdout) > 100_000_000:
        raise ScanError('command output too large')
    return result.stdout


def parse_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ScanError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ScanError('invalid JSON number')))
    except (ValueError, UnicodeError) as exc:
        raise ScanError('invalid JSON') from exc


def capture(mode, target, execute):
    if not target or target.startswith('-') or '\x00' in target:
        raise ScanError('invalid target')
    if mode == 'running':
        container = parse_json(execute(['docker', 'container', 'inspect', '--format',
                                        '{{json .Id}}', target]))
        if not isinstance(container, str) or not re.fullmatch(r'[0-9a-f]{64}', container):
            raise ScanError('invalid container ID')
        value = parse_json(execute(['docker', 'container', 'inspect', '--format',
                                    '{{json .Image}}', container]))
        running = parse_json(execute(['docker', 'container', 'inspect', '--format',
                                      '{{json .State.Running}}', container]))
        if running is not True:
            raise ScanError('container is not running')
    elif mode == 'candidate':
        value = parse_json(execute(['docker', 'image', 'inspect', '--format',
                                    '{{json .Id}}', target]))
    else:
        raise ScanError('invalid mode')
    if not isinstance(value, str) or not IMAGE_ID.fullmatch(value):
        raise ScanError('invalid captured image ID')
    return (value, container if mode == 'running' else None)


def report(data, image):
    obj = parse_json(data)
    if not isinstance(obj, dict) or type(obj.get('SchemaVersion')) is not int or obj['SchemaVersion'] != 2 or obj.get('ArtifactType') != 'container_image':
        raise ScanError('unsupported Trivy report')
    meta = obj.get('Metadata')
    if not isinstance(meta, dict) or not isinstance(meta.get('ImageID'), str) or not IMAGE_ID.fullmatch(meta['ImageID']):
        raise ScanError('invalid Trivy image identity')
    results = obj.get('Results')
    if not isinstance(results, list) or not results:
        raise ScanError('missing scan results')
    findings = []
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get('Target'), str) or not result['Target'] or not isinstance(result.get('Type'), str) or not result['Type']:
            raise ScanError('invalid scan result')
        vulns = result.get('Vulnerabilities', [])
        if not isinstance(vulns, list):
            raise ScanError('invalid vulnerability list')
        for vuln in vulns:
            if not isinstance(vuln, dict):
                raise ScanError('invalid vulnerability')
            for field in ('VulnerabilityID', 'PkgName', 'InstalledVersion', 'Severity'):
                if not isinstance(vuln.get(field), str) or not vuln[field]:
                    raise ScanError('invalid vulnerability field')
            if vuln['Severity'] not in SEVERITIES:
                raise ScanError('invalid severity')
            if vuln['Severity'] in ('HIGH', 'CRITICAL'):
                findings.append({'target': result['Target'], 'type': result['Type'],
                                 'id': vuln['VulnerabilityID'], 'package': vuln['PkgName'],
                                 'installed_version': vuln['InstalledVersion'], 'severity': vuln['Severity'],
                                 'fixed_version': vuln.get('FixedVersion', '')})
                if not isinstance(findings[-1]['fixed_version'], str):
                    raise ScanError('invalid fixed version')
    return findings


def verify_archive(archive, image):
    """Bind a single-image docker save archive to its inspected config digest."""
    try:
        with tarfile.open(archive, 'r:') as tar:
            members = tar.getmembers()
            manifests = [m for m in members if m.name == 'manifest.json' and m.isfile()]
            if len(manifests) != 1 or manifests[0].size > 1_000_000:
                raise ScanError('invalid archive manifest')
            manifest = parse_json(tar.extractfile(manifests[0]).read())
            if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
                raise ScanError('invalid archive image count')
            config = manifest[0].get('Config')
            if not isinstance(config, str) or not config:
                raise ScanError('invalid archive config reference')
            matches = [m for m in members if m.name == config and m.isfile()]
            if len(matches) != 1 or matches[0].size > 20_000_000:
                raise ScanError('invalid archive config')
            config_digest = 'sha256:' + hashlib.sha256(tar.extractfile(matches[0]).read()).hexdigest()
            if config_digest != image:
                # Newer Docker image stores identify images by an OCI index or manifest
                # digest, while the archive's config has a different digest.
                index_members = [m for m in members if m.name == 'index.json' and m.isfile()]
                if len(index_members) != 1 or index_members[0].size > 1_000_000:
                    raise ScanError('archive image identity mismatch')
                index = parse_json(tar.extractfile(index_members[0]).read())
                descriptors = index.get('manifests') if isinstance(index, dict) else None
                if not isinstance(descriptors, list) or not any(isinstance(d, dict) and d.get('digest') == image for d in descriptors):
                    raise ScanError('archive image identity mismatch')
                queue, seen, found = [image], set(), False
                while queue:
                    digest = queue.pop()
                    if digest in seen:
                        continue
                    if not isinstance(digest, str) or not IMAGE_ID.fullmatch(digest) or len(seen) >= 100:
                        raise ScanError('invalid archive descriptor')
                    seen.add(digest)
                    blobs = [m for m in members if m.name == 'blobs/sha256/' + digest[7:] and m.isfile()]
                    if len(blobs) != 1 or blobs[0].size > 20_000_000:
                        raise ScanError('invalid archive descriptor blob')
                    data = tar.extractfile(blobs[0]).read()
                    if 'sha256:' + hashlib.sha256(data).hexdigest() != digest:
                        raise ScanError('archive descriptor hash mismatch')
                    obj = parse_json(data)
                    if not isinstance(obj, dict):
                        raise ScanError('invalid archive descriptor object')
                    if isinstance(obj.get('config'), dict) and obj['config'].get('digest') == config_digest:
                        found = True
                    children = obj.get('manifests', [])
                    if not isinstance(children, list) or any(not isinstance(child, dict) or not isinstance(child.get('digest'), str) for child in children):
                        raise ScanError('invalid archive descriptors')
                    queue.extend(child['digest'] for child in children)
                if not found:
                    raise ScanError('archive config not linked to image')
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ScanError('invalid image archive') from exc


def scan(mode, target, execute=run):
    image, container = capture(mode, target, execute)
    pinned = parse_json(execute(['docker', 'image', 'inspect', '--format', '{{json .Id}}', TRIVY]))
    if pinned != TRIVY:
        raise ScanError('Trivy image identity mismatch')
    with tempfile.TemporaryDirectory(prefix='firecrawl-advisory-') as tmp:
        archive = os.path.join(tmp, 'image.tar')
        execute(['docker', 'image', 'save', '--output', archive, image])
        verify_archive(archive, image)
        # Docker's export is private by default; the unprivileged scanner needs read access.
        # TemporaryDirectory remains mode 0700, restricting access on the host.
        os.chmod(archive, 0o644)
        output = execute(['docker', 'run', '--rm', '--pull=never', '--read-only',
                          '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=256',
                          '--tmpfs', '/tmp:rw,nosuid,nodev,size=4g',
                          '--mount', 'type=bind,src=' + archive + ',dst=/scan/image.tar,readonly',
                          TRIVY, '--config', '/dev/null', '--cache-dir', '/tmp/trivy',
                          'image', '--input', '/scan/image.tar', '--scanners', 'vuln',
                          '--format', 'json', '--exit-code', '0', '--timeout', '15m'])
        findings = report(output, image)
    if mode == 'running' and capture(mode, target, execute) != (image, container):
        raise ScanError('running container image changed during scan')
    return {'status': 'findings' if findings else 'no_high_critical_findings',
            'mode': mode, 'image_id': image, 'scanner_image_id': TRIVY,
            'checked_at': datetime.now(timezone.utc).isoformat(), 'findings': findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['candidate', 'running'])
    parser.add_argument('target', help='local image reference or running container name/ID')
    args = parser.parse_args()
    try:
        result = scan(args.mode, args.target)
        print(json.dumps(result, sort_keys=True))
        return 1 if result['findings'] else 0
    except (ScanError, OSError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
