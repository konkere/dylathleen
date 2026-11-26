"""Background checks run under a transient DynamicUser, including native recipe evaluation."""
import hashlib
import json
import os
import re
import subprocess
import shutil
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

import requests

from dylathleen.gems.bin.client import BinCommand
from dylathleen.gems.debget import catalogue
from dylathleen.gems.debget.privileged import NATIVE, ROOT, package, secure, check_native_paths
from dylathleen.daemon.inventory import newer, inventory, persist

WORKER = '/usr/lib/dylathleen/check-updates'


def isolated_checks(data, folder):
    properties = ['DynamicUser=yes', 'PrivateDevices=yes', 'PrivateTmp=yes', 'ProtectSystem=strict',
                  'ProtectHome=yes', 'NoNewPrivileges=yes', 'ProtectKernelTunables=yes', 'ProtectControlGroups=yes',
                  'ProtectKernelModules=yes', 'RestrictSUIDSGID=yes', 'RestrictNamespaces=yes',
                  'RestrictAddressFamilies=AF_INET AF_INET6', 'SystemCallFilter=@system-service',
                  'InaccessiblePaths=-/usr/bin/apt -/usr/bin/apt-get -/usr/bin/aptitude -/usr/bin/add-apt-repository',
                  'MemoryMax=384M', 'TasksMax=48', 'RuntimeMaxSec=10min', 'LimitFSIZE=32M',
                  'TemporaryFileSystem=/:ro', 'ProtectProc=invisible', 'ProcSubset=pid',
                  'BindReadOnlyPaths=/usr /usr/bin:/bin /usr/lib:/lib -/usr/lib64:/lib64 '
                  '/etc/os-release -/etc/debian_version /etc/resolv.conf /etc/ssl/certs /var/lib/dpkg/status '
                  '-/etc/apt/sources.list -/etc/apt/sources.list.d -/var/lib/apt/lists '
                  f'{folder}/recipes:/etc/deb-get {folder}/cache:/seed',
                  'TemporaryFileSystem=/var/cache/deb-get:rw,mode=1777']
    cmd = ['/usr/bin/systemd-run', '--quiet', '--pipe', '--wait', '--collect', '--service-type=exec',
           *('--property=' + prop for prop in properties), WORKER]
    with TemporaryFile() as output:
        proc = subprocess.run(cmd, input=json.dumps(data).encode(), stdout=output, stderr=subprocess.DEVNULL, timeout=620)
        output.seek(0)
        response = output.read(1024 * 1024 + 1)
    if proc.returncode or len(response) > 1024 * 1024:
        raise OSError('Isolated update check failed')
    return json.loads(response)


def snapshot(data, folder):
    recipes = folder / 'recipes'
    local = recipes / '99-local.d'
    local.mkdir(parents=True)
    cache = folder / 'cache'
    cache.mkdir()
    manifests = {}
    github = {}
    check_native_paths()
    sources = catalogue.read_catalogue()
    installed = data.get('debget', {})
    for name in installed:
        recipe = next((r for r in sources.get(name, ()) if not r.deprecated), None)
        if recipe and recipe.repo != '00-builtin':
            directory = recipes / (recipe.repo + '.d')
            directory.mkdir(exist_ok=True)
            (directory / name).write_text(recipe.text)
            if recipe.repo != '99-local':
                manifests.setdefault(recipe.repo, []).append(name)
        if recipe:
            match = re.search(r'^\s*get_github_releases\s+"?([a-zA-Z0-9_-][a-zA-Z0-9_.-]*/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*)"?'
                              r'(?:\s+"?(latest)"?)?\s*$', recipe.text, re.M)
            if match:
                github[name] = (match[1], match[2])
    registry = ROOT / 'installed'
    rows = registry.read_text().splitlines() if secure(registry, missing=True) else []
    selected = [row for row in rows if row.split() and row.split()[0] in installed]
    (recipes / 'installed').write_text(''.join(row + '\n' for row in selected))
    for repo, names in manifests.items():
        (recipes / (repo + '.repo')).write_text('unused\n' + '\n'.join(names) + '\n')
    digest = hashlib.sha256(json.dumps([data.get('bins'), installed], sort_keys=True).encode())
    for path in sorted(recipes.rglob('*')):
        if path.is_file():
            digest.update(str(path.relative_to(recipes)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest(), github


def seed(cache, projects):
    token_file = Path('/etc/dylathleen/github-token')
    headers = {}
    if secure(token_file, missing=True):
        if token_file.stat().st_mode & 0o077:
            raise ValueError('Unsafe GitHub credential permissions')
        token = token_file.read_text().strip()
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,512}', token):
            raise ValueError('Invalid system GitHub credential')
        headers['Authorization'] = 'Bearer ' + token
    with requests.Session() as session:
        session.trust_env = False
        return fetch_releases(session, cache, projects, headers)


def fetch_releases(session, cache, projects, headers):
    responses = {}
    failures = set()
    rate_limited = False
    for name, (repo, latest) in projects.items():
        endpoint = f'https://api.github.com/repos/{repo}/releases' + ('/latest' if latest else '')
        if endpoint not in responses and rate_limited:
            failures.add('debget:' + name)
            continue
        if endpoint not in responses:
            try:
                with session.get(endpoint, headers=headers, timeout=(5, 15), stream=True, allow_redirects=False) as response:
                    rate_limited = response.status_code == 429 or response.headers.get('X-RateLimit-Remaining') == '0'
                    if response.status_code != 200:
                        raise ValueError('GitHub release metadata unavailable')
                    body = bytearray()
                    for block in response.iter_content(65536):
                        body.extend(block)
                        if len(body) > 4 * 1024 * 1024:
                            raise ValueError('Release metadata too large')
                releases = json.loads(body)
                if isinstance(releases, dict):
                    releases = [releases]
                # deb-get's default GitHub cache contains only matching jq output lines.
                responses[endpoint] = ''.join('      "browser_download_url": ' + json.dumps(a['browser_download_url']) + '\n'
                                              for r in releases for a in r['assets'] if '.deb' in a['browser_download_url'])
            except (ValueError, KeyError, OSError, requests.RequestException):
                responses[endpoint] = None
        if responses[endpoint] is None:
            failures.add('debget:' + name)
            continue
        (cache / (name + '.json_extract')).write_text(responses[endpoint])
    return failures


def refresh(data):
    keys = {'bin:' + k for k in data.get('bins', {})} | {'debget:' + n for n in data.get('debget', {})}
    with TemporaryDirectory(dir='/run', prefix='dylathleen-check-') as temp:
        folder = Path(temp)
        fingerprint, projects = snapshot(data, folder)
        now = time.time()
        if data.get('fingerprint') == fingerprint and 0 <= now - data.get('checked', 0) < 300:
            return
        versions = {k: v for k, v in data.get('updates', {}).items() if k in keys}
        stale = keys.copy()
        try:
            unavailable = seed(folder / 'cache', projects)
            work = {'bins': data.get('bins', {}), 'debget': {k: v for k, v in data.get('debget', {}).items()
                                                            if 'debget:' + k not in unavailable}}
            result = isolated_checks(work, folder) if any(work.values()) else {'updates': {}, 'stale': []}
            if not isinstance(result, dict) or not isinstance(result.get('updates'), dict) or not isinstance(result.get('stale'), list):
                raise ValueError('Malformed worker report')
            unknown = set(result['stale'])
            updates = result['updates']
            if (unknown - keys or updates.keys() - keys or updates.keys() & (unavailable | unknown) or
                    any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9.+:~_-]{1,200}', v) for v in updates.values())):
                raise ValueError('Unexpected check result')
            stale = unknown | unavailable
            versions = {**{k: v for k, v in versions.items() if k in stale}, **updates}
        except (ValueError, KeyError, TypeError, OSError, requests.RequestException, subprocess.SubprocessError):
            pass
        current = inventory(data)
        with TemporaryDirectory(dir=temp) as after:
            if snapshot(current, Path(after))[0] != fingerprint:
                current['stale'] = sorted({'bin:' + k for k in current.get('bins', {})} |
                                          {'debget:' + n for n in current.get('debget', {})})
                current.pop('fingerprint', None)
                persist(current)
                return
        data.update(updates=versions, stale=sorted(stale), checked=time.time(), fingerprint=fingerprint)
        persist(data)


def main():
    if os.geteuid() == 0:
        raise SystemExit('Background checks require an unprivileged account')
    os.environ.update(HOME='/tmp', PATH='/usr/sbin:/usr/bin:/sbin:/bin', DISABLE_APT='y', LC_ALL='C')
    for item in Path("/seed").iterdir():
        shutil.copyfile(item, Path("/var/cache/deb-get") / item.name)
    data = json.load(sys.stdin)
    versions, stale = {}, []
    for name, (installed, status, method) in data.get('debget', {}).items():
        if status != 'installed':
            continue
        key = 'debget:' + name
        try:
            if method in ('apt', 'ppa'):
                version = catalogue.apt_candidate(name)
            else:
                proc = subprocess.run([NATIVE, 'show', package(name)], stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=60)
                match = re.search(r'^\s*Published:\s*([^\s]+)\s*$', proc.stdout, re.M)
                if proc.returncode or not match:
                    raise ValueError()
                version = match[1]
            if not version:
                raise ValueError()
            if newer(version, installed):
                versions[key] = version
        except (ValueError, OSError, subprocess.SubprocessError):
            stale.append(key)
    for key, entry in data.get('bins', {}).items():
        if entry.get('pinned'):
            continue
        try:
            binary = next((p for p in ('/usr/local/bin/bin', '/usr/bin/bin') if os.access(p, os.X_OK)), None)
            if not binary:
                raise FileNotFoundError()
            with BinCommand(binary, entry) as command:
                update = command.check()
            if update:
                versions['bin:' + key] = update.version
        except Exception:
            stale.append('bin:' + key)
    print(json.dumps({'updates': versions, 'stale': stale}))
