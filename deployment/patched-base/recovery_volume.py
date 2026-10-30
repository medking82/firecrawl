"""Fail-closed volume identity checks for a future recovery cutover."""

DATA = '/var/lib/postgresql/data'


def postgres_volume(container):
    """Return the exact Docker volume name mounted at the PostgreSQL data path."""
    mounts = container.get('Mounts')
    if not isinstance(mounts, list):
        raise ValueError('missing container mounts')
    matches = [m for m in mounts if isinstance(m, dict) and m.get('Destination') == DATA]
    if len(matches) != 1:
        raise ValueError('expected exactly one PostgreSQL data mount')
    mount = matches[0]
    name = mount.get('Name')
    if mount.get('Type') != 'volume' or not isinstance(name, str) or not name or mount.get('RW') is not True:
        raise ValueError('PostgreSQL data mount is not a writable named Docker volume')
    return name


def verify_same_volume(before, after):
    original = postgres_volume(before)
    if postgres_volume(after) != original:
        raise ValueError('PostgreSQL volume identity changed')
    return original
