"""Release metadata and archive inspection for the GUI's explicit choices."""
import os
import re
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from tempfile import TemporaryFile
from urllib.parse import quote, urlsplit

import requests
from packaging.version import InvalidVersion, Version

from dylathleen.gems.bin.registry import BinError

REQUEST_TIMEOUT = (10, 30)
ARCHIVE_LIMIT = 512 * 1024 * 1024
EXCLUDED_ASSET = re.compile(
    r'(^|[-_.])(?:checksums?|sha(?:1|256|512)(?:sums?)?|md5(?:sums?)?|sources?|src|signatures?|attestations?)($|[-_.])'
    r'|\.(?:sig|asc|minisig|sign|pem|bundle|sbom)(?:\.json)?$', re.I)


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    api_url: str = ''


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    assets: tuple
    prerelease: bool = False


def release_asset(name):
    return bool(name and '/' not in name and not EXCLUDED_ASSET.search(name))


def github_token():
    token = os.environ.get('GITHUB_AUTH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    try:
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=5, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class Repository:
    def __init__(self, url, session=None, existing=False):
        url = url.strip()
        parsed = urlsplit(url if '://' in url else f'https://{url}')
        path = parsed.path.strip('/').split('/')
        host = parsed.netloc.lower()
        if (parsed.scheme != 'https' or host not in ('github.com', 'codeberg.org', 'gitlab.com')
                or len(path) < 2 or not existing and len(path) != 2
                or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) and part not in ('.', '..') for part in path[:2])):
            raise BinError('Enter an HTTPS repository URL from GitHub, Codeberg or GitLab.')
        self.host = host
        self.owner, self.name = path[:2]
        self.name = self.name.removesuffix('.git')
        self.url = f'https://{host}/{self.owner}/{self.name}'
        api_path = f'/repos/{self.owner}/{self.name}/releases'
        self.headers = {}
        if host == 'github.com':
            self.api = f'https://api.github.com{api_path}'
            token = github_token()
        elif host == 'codeberg.org':
            self.api = f'https://codeberg.org/api/v1{api_path}'
            token = os.environ.get('CODEBERG_TOKEN')
        else:
            project = quote(f'{self.owner}/{self.name}', safe='')
            self.api = f'https://gitlab.com/api/v4/projects/{project}/releases'
            token = os.environ.get('GITLAB_TOKEN_gitlab_com') or os.environ.get('GITLAB_TOKEN')
        if token:
            self.headers['Authorization'] = f'Bearer {token}'
        self.session = session or requests

    def _release(self, data):
        version = data['tag_name']
        tag = quote(version, safe='')
        prerelease = bool(data.get('prerelease'))
        if self.host == 'gitlab.com':
            url = f'{self.url}/-/releases/{tag}'
            links = data['assets']['links']
            try:
                prerelease = Version(version).is_prerelease
            except InvalidVersion:
                pass
            assets = (Asset(a['name'], a['url']) for a in links)
        else:
            url = f'{self.url}/releases/tag/{tag}'
            assets = (Asset(a['name'], a['browser_download_url'], a.get('url', '')) for a in data['assets'])
        return Release(version, url, tuple(a for a in assets if release_asset(a.name)), prerelease)

    def _get(self, url, **kwargs):
        response = self.session.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response

    def releases(self):
        result = []
        for page in range(1, 101):
            pagination = 'limit' if self.host == 'codeberg.org' else 'per_page'
            response = self._get(self.api, params={pagination: 50, 'page': page})
            items = response.json()
            result.extend(self._release(item) for item in items if not item.get('draft'))
            if not response.links.get('next') and (
                    self.host == 'github.com' or not items or len(items) < 50):
                break
        latest = next((release for release in result if not release.prerelease), None)
        if latest and self.host == 'github.com':
            # GitHub can designate a stable release that is not first in the list.
            latest = self._release(self._get(f'{self.api}/latest').json())
            if all(release.version != latest.version for release in result):
                result.insert(0, latest)
        return result, latest or (result[0] if result else None)

    def release(self, version):
        endpoint = '/' if self.host == 'gitlab.com' else '/tags/'
        return self._release(self._get(f'{self.api}{endpoint}{quote(version, safe="")}').json())

    def archive_members(self, asset):
        # Do not forward the repository token to arbitrary release links.
        download = asset.url
        if self.host == 'github.com' and self.headers and urlsplit(asset.api_url).netloc == 'api.github.com':
            download = asset.api_url
        url = urlsplit(download)
        if url.scheme != 'https':
            raise BinError('The release asset must use HTTPS.')
        trusted_hosts = {self.host, 'api.github.com'} if self.host == 'github.com' else {self.host}
        headers = {'Accept': 'application/octet-stream', **(self.headers if url.netloc in trusted_hosts else {})}
        with self.session.get(download, headers=headers, timeout=REQUEST_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            with TemporaryFile() as archive:
                for block in response.iter_content(1024 * 1024):
                    if archive.tell() + len(block) > ARCHIVE_LIMIT:
                        raise BinError('The archive exceeds the 512 MiB inspection limit.')
                    archive.write(block)
                archive.seek(0)
                return archive_members(archive)


def archive_members(stream):
    if zipfile.is_zipfile(stream):
        with zipfile.ZipFile(stream) as archive:
            members = [(entry.filename, entry.external_attr >> 16)
                       for entry in archive.infolist() if not entry.is_dir()
                       and (entry.external_attr >> 28) in (0, 8)]
    else:
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode='r|*') as archive:
            members = [(entry.name, entry.mode) for entry in archive if entry.isfile()]
    candidates = [(name, mode) for name, mode in members if name and not name.startswith('/')
                  and '..' not in PurePosixPath(name).parts and '\x00' not in name]
    executables = [name for name, mode in candidates if mode & 0o111]
    return sorted(set(executables or [name for name, _ in candidates]))


def same_variant(old_name, old_version, new_name, new_version):
    def strip_version(name, version):
        if version:
            pattern = r'(?<=[_.\-/])v?' + re.escape(version.removeprefix('v')) + r'(?=$|[_.\-/])'
            return re.sub(pattern, '{version}', name)
        return name
    return old_name == new_name or strip_version(old_name, old_version) == strip_version(new_name, new_version)
