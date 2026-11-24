"""Analyse forge release assets and generate deb-get Bash from verified rules."""
import hashlib
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote, urlsplit

import requests

from dylathleen.gems.debget.catalogue import PLATFORM
from dylathleen.gems.debget.privileged import DebGetError, package

ARCH = re.compile(r'(?:^|[_.-])(amd64|arm64|all|aarch64|x86_64)(?=\.|[-_]|$)', re.I)
SEGMENT = re.compile(r'[a-zA-Z0-9_.-]+\Z')
PRE = re.compile(r'(?:^|[\d._~+-])(?:alpha|beta|preview|pre|rc|nightly|dev)(?:[0-9._-]|$)', re.I)
VERSION = re.compile(r'[0-9][0-9A-Za-z.+:~\-]*\Z')
MAX_DEB_SIZE = 512 * 1024 * 1024
MARK = '{version}'
CHECKSUM_FILE = re.compile(r'(?:^|[-_.])(?:sha256sums?|checksums?)(?:\.txt)?$', re.I)
CHECKSUM_LINE = re.compile(r'^([0-9a-fA-F]{64})(?:\s+\*?(\S+))?\s*$')


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    tag: str
    digest: str = ''


@dataclass(frozen=True)
class Metadata:
    package: str
    version: str
    arch: str
    summary: str


@dataclass(frozen=True)
class Rule:
    codename: str
    arch: str
    pattern: str
    version_prefix: str
    version_suffix: str
    version_from_url: str = ''

    def matches(self, name):
        pattern = re.escape(self.pattern).replace(re.escape(MARK), r'([0-9][0-9A-Za-z.+:~\-]*)')
        return re.fullmatch(pattern, name)


class Forge:
    def __init__(self, url, token='', session=requests):
        parsed = urlsplit(url.strip())
        segments = parsed.path.strip('/').removesuffix('.git').split('/')
        if (parsed.scheme != 'https' or parsed.netloc not in {'github.com', 'gitlab.com'}
                or parsed.query or parsed.fragment or len(segments) < 2
                or not all(SEGMENT.fullmatch(s) and s not in {'.', '..'} for s in segments)
                or parsed.netloc == 'github.com' and len(segments) != 2):
            raise DebGetError('Use an HTTPS repository URL from GitHub or GitLab.')
        self.host = parsed.netloc
        self.project = '/'.join(segments)
        self.name = segments[-1]
        self.website = f'https://{self.host}/{self.project}'
        self.github = self.host == 'github.com'
        self.api = (f'https://api.github.com/repos/{self.project}' if self.github else
                    f'https://gitlab.com/api/v4/projects/{quote(self.project, safe="")}')
        self.session = session
        self.headers = {'Authorization': f'Bearer {token}'} if token and self.github else {}

    def _get(self, url, **kwargs):
        result = self.session.get(url, headers=self.headers, timeout=(8, 20), **kwargs)
        result.raise_for_status()
        return result.json()

    def analyse(self):
        project = self._get(self.api)
        raw = self._get(self.api + '/releases', params={'per_page': 20})
        if not isinstance(project, dict) or not isinstance(raw, list):
            raise DebGetError('The forge API returned unexpected project or release data.')
        samples = []
        for release in raw:
            if not isinstance(release, dict):
                continue
            tag = release.get('tag_name', '')
            if (not isinstance(tag, str) or not tag or release.get('draft') or release.get('prerelease')
                    or release.get('upcoming_release') or PRE.search(tag)):
                continue
            assets_data = release.get('assets')
            items = assets_data if self.github else assets_data.get('links') if isinstance(assets_data, dict) else None
            if not isinstance(items, list):
                continue
            assets = []
            checksums = {}
            for asset in items:
                if not isinstance(asset, dict):
                    continue
                url = (asset.get('browser_download_url') if self.github else
                       asset.get('direct_asset_url') or asset.get('url'))
                if not isinstance(url, str) or urlsplit(url).scheme != 'https':
                    continue
                filename = unquote(PurePosixPath(urlsplit(url).path).name)
                if filename.endswith(('.deb.sha256', '.deb.sha256sum')) or CHECKSUM_FILE.search(filename):
                    checksums[filename] = url
                if not filename.endswith('.deb'):
                    continue
                assets.append(Asset(filename, url, tag, asset.get('digest') or ''))
            if assets:
                if not samples:
                    assets = self._digests(assets, checksums)
                samples.append(assets)
            if len(samples) == 4:
                break
        if not samples:
            raise DebGetError('No DEB assets were found in recent stable releases.')
        description = project.get('description')
        return description if isinstance(description, str) else '', samples

    def _digests(self, assets, links):
        if not links or all(asset.digest for asset in assets):
            return assets
        checksums = {}
        asset_names = {a.name for a in assets if not a.digest}
        for filename, url in links.items():
            target = filename.removesuffix('.sha256').removesuffix('.sha256sum')
            if filename.endswith(('.deb.sha256', '.deb.sha256sum')) and target not in asset_names:
                continue
            with self.session.get(url, stream=True, timeout=(8, 15)) as response:
                response.raise_for_status()
                if urlsplit(response.url).scheme != 'https':
                    raise DebGetError('The checksum download redirected outside HTTPS.')
                data = bytearray()
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        raise DebGetError('The checksum list is too large.')
            for line in data.decode('utf-8', errors='replace').splitlines():
                match = CHECKSUM_LINE.fullmatch(line)
                if match:
                    name = (match[2] or target).removeprefix('./')
                    checksum = 'sha256:' + match[1].lower()
                    if name in checksums and checksums[name] != checksum:
                        raise DebGetError(f'Conflicting SHA256 sums for {name}.')
                    checksums[name] = checksum
        return [replace(asset, digest=asset.digest or checksums.get(asset.name, '')) for asset in assets]


def guessed_platform(asset):
    name = asset.name.lower()
    arch = ARCH.search(name.replace('x86_64', 'amd64'))
    architecture = arch[1] if arch else 'all'
    if architecture == 'aarch64':
        architecture = 'arm64'
    plat = ''
    for codename, (distro, release) in PLATFORM.items():
        pattern = rf'(?:^|[_.-])(?:{codename}|{distro}[-_]?{re.escape(release)})(?=[_.-]|$)'
        if re.search(pattern, name):
            plat = codename
            break
    if not plat:
        distro_only = re.search(r'(?:^|[_.-])(debian|ubuntu)(?=[_.-]|$)', name)
        if distro_only:
            plat = distro_only[1]
    return plat, architecture


def inspect_asset(asset, session=requests):
    parsed = urlsplit(asset.url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise DebGetError('Invalid HTTPS DEB URL.')
    with TemporaryDirectory(prefix='dylathleen-debget-inspect-') as directory:
        path = Path(directory) / 'candidate.deb'
        checksum = hashlib.sha256()
        size = 0
        start = time.monotonic()
        with session.get(asset.url, stream=True, timeout=(8, 20)) as response:
            response.raise_for_status()
            if urlsplit(response.url).scheme != 'https':
                raise DebGetError('The DEB download redirected outside HTTPS.')
            with path.open('wb') as file:
                for block in response.iter_content(chunk_size=1024 * 128):
                    size += len(block)
                    if size > MAX_DEB_SIZE or time.monotonic() - start > 180:
                        raise DebGetError('The package exceeded the inspection size or time limit.')
                    checksum.update(block)
                    file.write(block)
        if asset.digest and asset.digest != 'sha256:' + checksum.hexdigest():
            raise DebGetError('DEB checksum does not match the release digest.')
        output = subprocess.check_output(['/usr/bin/dpkg-deb', '-f', str(path), 'Package', 'Version', 'Architecture', 'Description'],
                                         timeout=20, stderr=subprocess.PIPE, text=True)
        data = dict(line.split(': ', 1) for line in output.splitlines() if ': ' in line and not line.startswith(' '))
        name = package(data['Package'])
        version = data['Version']
        arch = data['Architecture']
        if arch not in {'amd64', 'arm64', 'all'} or not VERSION.fullmatch(version):
            raise DebGetError('The DEB has an unsupported architecture or version.')
        return Metadata(name, version, arch, data.get('Description', name))


def infer_rule(asset, info, codename, selected_arch, previous):
    valid_codenames = {'', 'debian', 'ubuntu', *PLATFORM}
    if codename not in valid_codenames or selected_arch not in ('amd64', 'arm64', 'all'):
        raise DebGetError('Invalid release platform.')
    if info.arch not in ('all', selected_arch) or selected_arch == 'all' and info.arch != 'all':
        raise DebGetError('The selected platform does not match the DEB architecture.')
    release_version = asset.tag.removeprefix('v')
    if not VERSION.fullmatch(release_version):
        raise DebGetError('The tag version needs a manual recipe.')
    # Prefer a version present in both the asset and its actual dpkg metadata.
    possibilities = [info.version.split(':')[-1], release_version]
    possibilities.extend(re.split('[~+\-]', info.version.split(':')[-1])[:1])
    for value in dict.fromkeys(possibilities):
        if value and value in asset.name and value in info.version:
            prefix, suffix = info.version.split(value, 1)
            rule = Rule(codename, selected_arch, asset.name.replace(value, MARK, 1), prefix, suffix)
            if not previous or all(sum(bool(rule.matches(candidate.name)) for candidate in items) <= 1 for items in previous):
                return rule

    for match in re.finditer(re.escape(quote(asset.tag, safe='')), urlsplit(asset.url).path):
        if release_version in info.version:
            prefix, suffix = info.version.split(release_version, 1)
            before = urlsplit(asset.url).path[:match.start()]
            after = urlsplit(asset.url).path[match.end():]
            if before.endswith('/') and after.startswith('/'):
                path = before + ('v' if asset.tag.startswith('v') else '') + MARK + after
                return Rule(codename, selected_arch, asset.name, prefix, suffix, path)
    raise DebGetError('Cannot infer how the asset name and package version change. Edit the rule or recipe manually.')


def regex_literal(value):
    return re.sub(r'([.\[\]{}()*+?^$|\\])', r'\\\1', value)


def recipe(forge, name, pretty, summary, rules):
    name = package(name)
    if not rules or len({(rule.codename, rule.arch) for rule in rules}) != len(rules):
        raise DebGetError('Select exactly one rule for each platform.')
    # Exact release/architecture first, generic fallbacks last.
    rules = sorted(rules, key=lambda r: (not bool(r.codename), r.codename not in PLATFORM, r.arch == 'all'))
    archs = ('amd64', 'arm64') if any(r.arch == 'all' for r in rules) else tuple(sorted({r.arch for r in rules}))
    includes = sorted({code for r in rules for code in (
        [r.codename] if r.codename in PLATFORM else [key for key, value in PLATFORM.items() if value[0] == r.codename]
    )})
    versions = '[0-9][0-9A-Za-z.+:~-]*'
    lines = ['DEFVER=1', 'ARCHS_SUPPORTED=' + shlex.quote(' '.join(archs)),
             'PRETTY_NAME=' + shlex.quote(pretty), 'WEBSITE=' + shlex.quote(forge.website), 'SUMMARY=' + shlex.quote(summary)]
    if includes and all(r.codename for r in rules):
        lines.append('CODENAMES_SUPPORTED=' + shlex.quote(' '.join(includes)))
    helper = 'get_github_releases' if forge.github else 'get_gitlab_releases'
    lines.extend([f'{helper} {shlex.quote(forge.project)} latest', 'if [ "${ACTION}" != prettylist ] && [ -s "${CACHE_FILE}" ]; then',
                  '    dg_pattern= dg_prefix= dg_suffix= dg_url_pattern=',
                  '    case "${UPSTREAM_CODENAME}/${HOST_ARCH}" in'])
    for rule in rules:
        codes = [rule.codename]
        if rule.codename in ('debian', 'ubuntu'):
            codes = [k for k, v in PLATFORM.items() if v[0] == rule.codename]
        choices = '|'.join((code or '*') + '/' + ('*' if rule.arch == 'all' else rule.arch) for code in codes)
        pattern = '^' + regex_literal(rule.pattern).replace(regex_literal(MARK), f'({versions})') + '$'
        url_pattern = regex_literal(rule.version_from_url).replace(regex_literal(MARK), f'({versions})') + '$' if rule.version_from_url else ''
        lines.append(f'        {choices}) dg_pattern={shlex.quote(pattern)} dg_prefix={shlex.quote(rule.version_prefix)}'
                     f' dg_suffix={shlex.quote(rule.version_suffix)} dg_url_pattern={shlex.quote(url_pattern)} ;;')
    extract = '''sed -nE 's/^[[:space:]]*"browser_download_url": "([^"]+)".*/\\1/p' "${CACHE_FILE}"''' if forge.github else 'cat "${CACHE_FILE}"'
    lines.extend(['    esac', '    dg_count=0', '    while IFS= read -r dg_url || [[ -n "${dg_url}" ]]; do',
                  '        [[ "${dg_url}" == https://* ]] || continue',
                  '        dg_file="${dg_url##*/}"',
                  '        if [[ -n "${dg_pattern}" && "${dg_file}" =~ ${dg_pattern} ]]; then',
                  '            dg_version="${BASH_REMATCH[1]}"',
                  '            if [[ -n "${dg_url_pattern}" ]]; then',
                  '                [[ "${dg_url}" =~ ${dg_url_pattern} ]] || continue',
                  '                dg_version="${BASH_REMATCH[1]}"',
                  '            fi',
                  '            [[ "${dg_version}" =~ ^[0-9] ]] || continue',
                  "            dg_prerelease='(^|[0-9._~+-])(alpha|beta|preview|pre|rc|nightly|dev)([0-9._-]|$)'",
                  '            [[ "${dg_version,,}" =~ ${dg_prerelease} ]] && continue',
                  '            URL="${dg_url}" VERSION_PUBLISHED="${dg_prefix}${dg_version}${dg_suffix}"',
                  '            ((dg_count+=1))', '        fi',
                  '    done < <(' + extract + ')',
                  '    if (( dg_count != 1 )); then URL= VERSION_PUBLISHED=; fi', 'fi', ''])
    return '\n'.join(lines)


def check_rule(rule, asset, metadata, releases):
    if rule.arch not in ('amd64', 'arm64', 'all') or metadata.arch not in ('all', rule.arch):
        raise DebGetError('The rule cannot use this architecture.')
    if bool(MARK in rule.pattern) == bool(MARK in rule.version_from_url):
        raise DebGetError('Put one {version} placeholder in the filename or URL rule.')
    if rule.pattern.count(MARK) > 1 or rule.version_from_url.count(MARK) > 1:
        raise DebGetError('Only one {version} placeholder is allowed.')
    match = rule.matches(asset.name)
    if rule.version_from_url:
        path_pattern = re.escape(rule.version_from_url).replace(re.escape(MARK), r'([0-9][0-9A-Za-z.+:~\-]*)')
        version = re.search(path_pattern + '$', urlsplit(asset.url).path)
    else:
        version = match
    if not match or not version or rule.version_prefix + version[1] + rule.version_suffix != metadata.version:
        raise DebGetError('The rule does not produce the actual filename and package version.')
    for release in releases:
        if sum(bool(rule.matches(other.name)) for other in release) > 1:
            raise DebGetError('The rule matches multiple assets in a recent stable release.')
