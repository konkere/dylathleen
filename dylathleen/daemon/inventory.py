"""Public inventory. Never serialize native registry extensions or recipe text."""
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from dylathleen.gems.bin import registry
from dylathleen.gems.debget import catalogue
from dylathleen.gems.debget.privileged import atomic_file, secure

STATE = Path('/var/lib/dylathleen')
CACHE = STATE / 'inventory.json'
FIELDS = {'path', 'url', 'remote_name', 'version', 'hash', 'provider', 'package_path', 'selected_asset', 'pinned'}


def public_entry(raw):
    entry = {k: v for k, v in raw.items() if k in FIELDS and isinstance(v, (bool, str))}
    url = entry.get('url', '')
    parsed = urlsplit(url if '://' in url else 'https://' + url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        entry.pop('url', None)
    return entry


def load(path=CACHE):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def inventory(previous):
    result = dict(previous)
    errors = {}
    try:
        data = registry.transact(registry.config_path(), {'operation': 'read'}, system=True)
        result['bins'] = {key: public_entry(entry) for key, entry in data['bins'].items()}
    except (ValueError, OSError, registry.BinError):
        errors['bin'] = 'System bin inventory is stale.'
    try:
        secure(catalogue.ROOT / 'installed', missing=True)
        registered = catalogue.registered()
        result['debget'] = {name: [*status, registered[name]] for name, status
                            in catalogue.installed_versions(registered).items()}
    except Exception:
        errors['debget'] = 'System deb-get inventory is stale.'
    updates = dict(previous.get('updates', {}))
    stale = set(previous.get('stale', ()))
    for source, prefix in [('bins', 'bin:'), ('debget', 'debget:')]:
        current = result.get(source, {})
        for key in previous.get(source, {}).keys() | current.keys():
            if current.get(key) != previous.get(source, {}).get(key):
                updates.pop(prefix + key, None)
                if key in current:
                    stale.add(prefix + key)
                else:
                    stale.discard(prefix + key)
    result.update(updates=updates, stale=sorted(stale))
    result['errors'] = errors
    return result


def persist(data):
    secure(STATE, directory=True)
    if load() != data:
        atomic_file(CACHE, json.dumps(data), 0o644)


def newer(version, installed):
    if not re.fullmatch(r'[0-9][0-9A-Za-z.+:~\-]{0,199}', version):
        raise ValueError('Invalid published Debian version')
    return subprocess.run(['/usr/bin/dpkg', '--compare-versions', version, 'gt', installed], timeout=5).returncode == 0
