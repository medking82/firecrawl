"""Stable container identity for monitor upgrade preflight and acceptance."""

STATE_FIELDS = ('Running', 'Status', 'StartedAt', 'FinishedAt', 'ExitCode', 'OOMKilled', 'Restarting')


def stable_summary(summary):
    result = {}
    for service, row in summary.items():
        state = row['state']
        if any(key not in state for key in STATE_FIELDS):
            raise ValueError('container state is incomplete: ' + service)
        stable = {key: state[key] for key in STATE_FIELDS}
        health = state.get('Health')
        if health is not None:
            if not isinstance(health, dict) or health.get('Status') != 'healthy' or health.get('FailingStreak') != 0:
                raise ValueError('container health is not stable and healthy: ' + service)
            stable['Health'] = {'Status': health['Status']}
        result[service] = {'id': row['id'], 'image': row['image'], 'state': stable,
                           'mounts': sorted(row['mounts'], key=lambda mount: mount['Destination'])}
    return result
