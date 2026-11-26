"""Authorized transaction handlers. The peer never selects a command or helper path."""
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from dylathleen.daemon.inventory import public_entry, FIELDS
from dylathleen.gems.bin import registry
from dylathleen.gems.debget import privileged as dg

ACTIONS = {'update', 'catalogue', 'upgrade', 'install', 'upgrade-one', 'remove', 'reinstall', 'purge', 'show'}
RECIPES = {'backups', 'restore', 'delete', 'save'}


def debget(request, peer):
    # Keep the helper and its APT hooks from the same package generation throughout dpkg unpack.
    with TemporaryDirectory(dir='/run', prefix='dylathleend-') as temp, peer, peer.makefile('rb') as reader:
        helper = Path(temp) / 'privileged.py'
        shutil.copyfile(dg.__file__, helper)
        peer.settimeout(120)
        credential = reader.readline(515)
        if not credential.endswith(b'\n'):
            raise ValueError('Client disconnected before starting the operation')
        token = credential[:-1].decode('ascii')
        if token and not re.fullmatch(r'[a-zA-Z0-9_-]{1,512}', token):
            raise ValueError('Invalid token')
        request['token'] = token
        with subprocess.Popen(['/usr/bin/python3', '-I', str(helper)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dg.ENV) as proc:
            proc.stdin.write(json.dumps(request).encode() + b'\n')
            proc.stdin.flush()
            connected = True
            peer.settimeout(5)
            for line in proc.stdout:
                if token:
                    line = line.replace(token.encode(), b'[redacted]')
                if connected:
                    try:
                        peer.sendall(line)
                    except OSError:
                        connected = False
                prompt = False
                if line.startswith(dg.MARKER.encode()):
                    prompt = json.loads(line[len(dg.MARKER):]).get('event') == 'transaction'
                eula = re.search(rb'Do you agree to the .+ EULA\?', line)
                if prompt or eula:
                    answer = b''
                    if connected:
                        peer.settimeout(180)
                        try:
                            answer = reader.readline(8)
                        except OSError:
                            connected = False
                        peer.settimeout(5)
                    approved = answer == (b'yes\n' if prompt else b'1\n')
                    proc.stdin.write((b'yes\n' if approved else b'no\n') if prompt else
                                     (b'1\n' if approved else b'2\n'))
                    proc.stdin.flush()
            if proc.wait():
                raise ValueError('deb-get transaction failed')


def destination(value, trusted=()):
    path = registry.absolute_path(value)
    if str(path) not in trusted and path != Path('/usr/local/bin') and not (
            path.name == 'bin' and path.is_relative_to('/opt') and len(path.parts) >= 4):
        raise ValueError('Choose /usr/local/bin or /opt/<directory>/bin for system binaries')
    return path


def allowed_source(url, previous):
    if not url or public_entry({'url': url}).get('url') != url:
        return False
    parsed = urlsplit(url if '://' in url else 'https://' + url)
    forge = parsed.scheme == 'https' and parsed.netloc in ('github.com', 'gitlab.com', 'codeberg.org')
    if forge and (not re.fullmatch(r'/[\w.-]+/[\w./%+~-]+', parsed.path)
                  or any(part in ('.', '..') for part in unquote(parsed.path).split('/'))
                  or not all(re.fullmatch(r'[\w.-]+', part) for part in parsed.path.split('/')[1:3])):
        return False
    if not previous:
        return forge
    old = urlsplit(previous if '://' in previous else 'https://' + previous)
    if parsed.scheme == old.scheme == 'https' and parsed.netloc and parsed.netloc == old.netloc:
        if parsed.netloc == 'gitlab.com':
            return parsed.path.partition('/-/')[0].rstrip('/') == old.path.partition('/-/')[0].rstrip('/')
        return not forge or parsed.path.split('/')[:3] == old.path.split('/')[:3]
    return False


def bin_change(action, key, expected, entry, folder, artifact=None):
    path = registry.config_path()
    current = registry.transact(path, {'operation': 'read'}, system=True)
    trusted = {str(registry.expanded_path(k).parent) for k in current['bins']}
    if current.get('default_path'):
        trusted.add(str(registry.expanded_path(current['default_path'])))
    allowlist = Path('/etc/dylathleen/bin-directories')
    if dg.secure(allowlist, missing=True):
        trusted.update(allowlist.read_text().splitlines())
    destination(folder, trusted)
    if action == 'migrate':
        registry.transact(path, {'operation': 'migrate', 'directory': folder}, system=True)
        return
    if action not in ('install', 'update', 'remove', 'pin', 'unpin'):
        raise ValueError('Invalid bin operation')
    target = registry.expanded_path(key) if key in current['bins'] else registry.absolute_path(key)
    destination(str(target.parent), trusted)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._+-]{0,199}', target.name):
        raise ValueError('Invalid binary name')
    raw = current['bins'].get(key)
    if (public_entry(raw) if raw is not None else {}) != expected or (raw is None) != (action == 'install'):
        raise ValueError('System registry changed. Refresh and retry.')
    if action in ('install', 'update'):
        required_seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
        if (not artifact or not stat.S_ISREG(os.fstat(artifact.fileno()).st_mode)
                or os.fstat(artifact.fileno()).st_size > 512 * 1024 * 1024
                or fcntl.fcntl(artifact, fcntl.F_GET_SEALS) & required_seals != required_seals):
            raise ValueError('Expected an immutable binary artifact')
        if (not re.fullmatch('[0-9a-f]{64}', entry.get('hash', '')) or set(entry) - FIELDS
                or not allowed_source(entry.get('url', ''), (raw or {}).get('url'))):
            raise ValueError('Invalid binary metadata or repository')
        if action == 'install' and target.parent != Path(folder):
            raise ValueError('Destination does not match installation directory')
        replacement = {**(raw or {}), **entry, 'path': key}
    elif action == 'remove':
        replacement = None
    else:
        replacement = {**raw, 'pinned': action == 'pin'}
    registry.transact(path, {'operation': 'entry', 'key': key, 'directory': folder, 'expected': raw,
                             'entry': replacement}, artifact, system=True)
