"""Read the native registry and recipe manifests without sourcing Bash."""
import platform
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from dylathleen.gems.debget.privileged import PKG, ROOT, DebGetError, recipe_text, registered

PLATFORM = {
    'bookworm': ('debian', '12'), 'trixie': ('debian', '13'),
    'jammy': ('ubuntu', '22.04'), 'noble': ('ubuntu', '24.04'), 'resolute': ('ubuntu', '26.04'),
}
EDITABLE_FIELDS = {'ARCHS_SUPPORTED', 'CODENAMES_SUPPORTED', 'PRETTY_NAME', 'SUMMARY', 'WEBSITE',
                   'APT_REPO_URL', 'PPA', 'URL', 'VERSION_PUBLISHED', 'METHOD'}
ASSIGNMENT = re.compile(r'^\s*(' + '|'.join(sorted(EDITABLE_FIELDS)) + r')=(.*)$')
FORGE_METHOD = re.compile(r'^\s*get_(github_releases|gitlab_releases|website)\b', re.M)


@dataclass
class Recipe:
    name: str
    repo: str
    text: str
    fields: dict = field(default_factory=dict)
    deprecated: bool = False

    @property
    def method(self):
        if self.fields.get('PPA') or re.search(r'^\s*PPA=.+', self.text, re.M):
            return 'ppa'
        if self.fields.get('APT_REPO_URL') or re.search(r'^\s*APT_REPO_URL=.+', self.text, re.M):
            return 'apt'
        forge = FORGE_METHOD.search(self.text)
        return forge[1].split('_')[0] if forge else self.fields.get('METHOD', 'direct')

    @property
    def release_repository(self):
        forge = FORGE_METHOD.search(self.text)
        if not forge or forge[1] not in ('github_releases', 'gitlab_releases'):
            return None
        line = self.text[forge.end():].partition('\n')[0]
        try:
            args = shlex.split(line, comments=True)
        except ValueError:
            return None
        if args:
            project = unquote(args[0])
            if re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', project):
                return f'https://{forge[1].split("_")[0]}.com/{project}'
            if forge[1] == 'gitlab_releases' and re.fullmatch(r'https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_.-]+){2,}', project):
                return project
        return None

    def compatible(self, architecture, codename, distro):
        arches = self.fields.get('ARCHS_SUPPORTED', 'amd64').split()
        codenames = self.fields.get('CODENAMES_SUPPORTED', '').split()
        return ('all' in arches or architecture in arches) and (not codenames or codename in codenames) and (
            self.method != 'ppa' or distro == 'ubuntu')


def fields(source):
    values = {}
    dynamic = {'$', '`', '\\'}
    for line in source.splitlines():
        match = ASSIGNMENT.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key in values or any(symbol in value for symbol in dynamic):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            continue
        if len(parts) == 1:
            values[key] = parts[0]
    return values


@lru_cache(maxsize=1)
def machine():
    release = platform.freedesktop_os_release()
    return (subprocess.check_output(['/usr/bin/dpkg', '--print-architecture'], text=True, timeout=5).strip(),
            release.get('UBUNTU_CODENAME') or release.get('DEBIAN_CODENAME') or release.get('VERSION_CODENAME', ''),
            release.get('ID') if release.get('ID') in ('ubuntu', 'debian') else
            'ubuntu' if 'ubuntu' in release.get('ID_LIKE', '').split() else 'debian')


def read_catalogue(root=ROOT):
    result = {'deb-get': [Recipe('deb-get', '00-builtin',
                                'PRETTY_NAME=deb-get\nWEBSITE=https://github.com/wimpysworld/deb-get\n'
                                'ARCHS_SUPPORTED="amd64 arm64 armhf i386"\nget_github_releases "wimpysworld/deb-get"\n',
                                {'PRETTY_NAME': 'deb-get', 'WEBSITE': 'https://github.com/wimpysworld/deb-get',
                                 'ARCHS_SUPPORTED': 'amd64 arm64 armhf i386'})]}
    if not root.is_dir() or root.is_symlink():
        return result

    def read_repo(repo, names):
        directory = root / (repo + '.d')
        if not directory.is_dir() or directory.is_symlink():
            return
        for raw in names:
            name = raw.lstrip('#')
            if not PKG.fullmatch(name):
                continue
            try:
                source = recipe_text(directory / name)
            except (DebGetError, OSError, UnicodeError):
                continue
            result.setdefault(name, []).append(Recipe(name, repo, source, fields(source), raw.startswith('#')))

    # Upstream puts 99-local first, followed by reverse-sorted external manifests.
    local = root / '99-local.d'
    if local.is_dir() and not local.is_symlink():
        read_repo('99-local', sorted(path.name for path in local.iterdir()))
    for manifest in sorted(root.glob('*.repo'), reverse=True):
        if (manifest.stem in {'00-builtin', '99-local'} or manifest.is_symlink()
                or not re.fullmatch(r'[a-zA-Z0-9_.-]+', manifest.stem)):
            continue
        try:
            lines = recipe_text(manifest).splitlines()[1:]
        except (DebGetError, OSError, UnicodeError):
            continue
        read_repo(manifest.stem, lines)
    if len(result['deb-get']) > 1:
        result['deb-get'].append(result['deb-get'].pop(0))
    return result


def installed_versions(names):
    if not names:
        return {}
    result = subprocess.run(['/usr/bin/dpkg-query', '-W', '-f=${Package}\t${Version}\t${db:Status-Status}\n', '--', *names],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    statuses = {}
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[0] in names and parts[2] in {'installed', 'config-files'}:
            statuses[parts[0]] = (parts[1], parts[2])
    return statuses


def apt_candidate(name):
    result = subprocess.run(['/usr/bin/apt-cache', 'policy', name], stdin=subprocess.DEVNULL, capture_output=True,
                            env={'LC_ALL': 'C'}, text=True, timeout=10)
    match = re.search(r'^\s+Candidate:\s+(\S+)', result.stdout, re.M)
    return match[1] if match and match[1] != '(none)' else None


def inventory(root=ROOT):
    catalogue = read_catalogue(root)
    registry = registered(root)
    return catalogue, registry, installed_versions(registry)
