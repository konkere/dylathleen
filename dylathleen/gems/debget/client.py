import os
import re
import stat
import subprocess
from pathlib import Path

from dylathleen.gems.debget.privileged import DebGetError, MARKER, NATIVE

ESCAPES = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
UPDATE = re.compile(r'([a-z0-9][a-z0-9+.-]+) \((\S+)\) has an update pending\. (\S+) is available\.')
EULA = re.compile(r'Do you agree to the .+ EULA\?')


class Cancelled(DebGetError):
    pass


def native_version():
    result = subprocess.run([NATIVE, 'version'], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    versions = re.findall(r'^\d+\.\d+\.\d+$', result.stdout, re.M)
    if result.returncode or not versions:
        raise DebGetError('Cannot determine the deb-get version.')
    return versions[-1]


def user_token():
    token = os.environ.get('DEBGET_TOKEN')
    if token:
        return token
    path = Path.home() / '.config/deb-get/github-token'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ''
    with os.fdopen(descriptor) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DebGetError('The GitHub token file must be a private regular file owned by your user.')
        return file.read(1024).strip()


PROGRESS = re.compile(r'\b(\d{1,3})%(?:\s|$)')


def invoke(request, watcher, confirm, password=None):
    from dylathleen.daemon.client import debget
    try:
        return debget(request, user_token() if request['operation'] == 'native' else '', watcher, confirm)
    except Cancelled:
        raise
    except Exception as error:
        raise DebGetError(str(error)) from error


def pending_updates(lines):
    result = {}
    for line in lines:
        match = UPDATE.search(line)
        if match:
            name, _, version = match.groups()
            result[name] = version
    return result


def parse_show(lines):
    result = {}
    for line in lines:
        key, separator, value = line.partition(':')
        if separator and key in ('Package', 'Repository', 'Updater', 'Installed', 'Published', 'Architecture',
                                 'Download', 'Website', 'Summary', 'Launchpad'):
            result[key] = value.strip()
    return result
