"""Stop only an identified monitor upgrade candidate after failed postflight."""
import re


def stop_candidate(owner, expected_image):
    project = 'firecrawl-monitor'
    ids = owner.run(['docker', 'ps', '-q', '--no-trunc', '--filter', 'status=running',
                     '--filter', 'label=com.docker.compose.project=' + project,
                     '--filter', 'label=com.docker.compose.service=api']).splitlines()
    if not ids:
        return 'no-running-api'
    if len(ids) != 1 or not re.fullmatch(r'[a-f0-9]{64}', ids[0]):
        raise owner.Stop('ambiguous monitor API; candidate not stopped')
    row = owner.inspect('container', ids[0])
    labels = row.get('Config', {}).get('Labels') or {}
    if (row.get('Id') != ids[0] or row.get('Image') != expected_image
            or row.get('State', {}).get('Running') is not True
            or labels.get('com.docker.compose.project') != project
            or labels.get('com.docker.compose.service') != 'api'):
        raise owner.Stop('monitor API identity changed; candidate not stopped')
    owner.run(['docker', 'stop', '--time', '60', ids[0]], timeout=90)
    after = owner.inspect('container', ids[0])
    if after.get('Id') != ids[0] or after.get('Image') != expected_image or after.get('State', {}).get('Running') is not False:
        raise owner.Stop('candidate stop could not be verified')
    return 'candidate-stopped'
