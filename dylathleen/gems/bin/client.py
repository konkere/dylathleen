"""Invoke bin with a private, single-entry registry and closed stdin."""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

from dylathleen.commons.system import password_input
from dylathleen.gems.bin.registry import BinError, config_path, read_registry, transact


class ChoiceRequired(BinError):
    pass


class SystemAccessRequired(BinError):
    pass


@dataclass(frozen=True)
class Update:
    version: str
    url: str


def literal_pattern(name):
    return re.sub(r'([*?\[\]\\])', r'\\\1', name)


class BinCommand:
    def __init__(self, executable, entry=None):
        self.executable = executable
        self.entry = entry

    def __enter__(self):
        self.temp = TemporaryDirectory(prefix='dylathleen-bin-')
        folder = Path(self.temp.name)
        self.registry = folder / 'config.json'
        self.target = folder / 'binary'
        self.registry.write_text(json.dumps({'default_path': str(folder), 'bins': {
            str(self.target): {**self.entry, 'path': str(self.target)}
        } if self.entry else {}}))
        return self

    def __exit__(self, *args):
        self.temp.cleanup()

    def run(self, *args):
        env = {**os.environ, 'BIN_CONFIG': str(self.registry), 'NO_COLOR': '1'}
        env.pop('CI', None)
        with subprocess.Popen([self.executable, *args], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors='replace', start_new_session=True) as proc:
            try:
                output, _ = proc.communicate(timeout=180)
            except subprocess.TimeoutExpired as error:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise BinError('bin timed out.') from error
        return proc.returncode, output

    def check(self):
        code, output = self.run('update', '--dry-run')
        if code == 0:
            return None
        if code == 3:
            match = re.search(re.escape(str(self.target)) + r' .*? -> (.+?) \((.*)\)\s*$', output, re.M)
            if match:
                return Update(*match.groups())
        raise BinError(output.strip() or f'bin update --dry-run exited with status {code}')

    def _execute(self, *args):
        code, output = self.run(*args)
        # Upstream may accept its default choice on EOF and exit successfully.
        if ('Select an option' in output or 'Could not find any compatible files' in output
                or 'no files found in tar archive' in output.lower()
                or 'no files found in zip archive' in output.lower()):
            raise ChoiceRequired(output.strip())
        if code:
            raise BinError(output.strip() or f'bin {args[0]} exited with status {code}')
        return read_registry(self.registry)['bins'].get(str(self.target))

    def install(self, url, asset, member=None):
        pattern = literal_pattern(asset)
        if member:
            pattern += '/' + literal_pattern(member)
        entry = self._execute('install', '--all', '--name', pattern, '--', url, str(self.target))
        if not entry or entry.get('selected_asset') != asset or (member and entry.get('package_path') != member):
            raise BinError('bin did not select the requested release file.')
        return entry

    def update(self):
        entry = self._execute('update', '--yes')
        if not entry or not self.target.is_file():
            raise BinError('bin did not install an update. Refresh and retry.')
        return entry

    def set_state(self, command):
        # getBinPath requires an executable existing at an absolute path.
        # bin acts only on this placeholder and the temporary registry.
        self.target.touch()
        self.target.chmod(0o700)
        entry = self._execute(command, str(self.target))
        if (command == 'remove' and entry is not None
                or command != 'remove' and (not entry or bool(entry.get('pinned')) != (command == 'pin'))):
            raise BinError(f'bin did not complete {command}.')
        return entry


class BinRegistry:
    def __init__(self, system=False):
        self.system = system

    def _call(self, request, artifact=None, password=None):
        if not self.system:
            if artifact is None:
                return transact(config_path(), request)
            with artifact.open('rb') as source:
                return transact(config_path(), request, source)

        helper = str(Path(__file__).with_name('registry.py').resolve())
        command = ['/usr/bin/python3', '-I', helper]
        elevated = os.geteuid() != 0
        if elevated:
            command = ['sudo', '-n', '-H', '--', *command]

        with TemporaryFile() as payload:
            payload.write(json.dumps(request).encode() + b'\n')
            if artifact is not None:
                with artifact.open('rb') as source:
                    shutil.copyfileobj(source, payload)
            payload.seek(0)
            result = subprocess.run(command, stdin=payload, capture_output=True, timeout=180)
            if elevated and not result.stdout and isinstance(password, str):
                with password_input(password) as stdin:
                    auth = subprocess.run(['sudo', '-S', '-v'], stdin=stdin, capture_output=True, timeout=30)
                if auth.returncode == 0:
                    payload.seek(0)
                    result = subprocess.run(command, stdin=payload, capture_output=True, timeout=180)
                else:
                    result = auth

        if elevated and not result.stdout:
            raise SystemAccessRequired(result.stderr.decode(errors='replace').strip())
        response = json.loads(result.stdout)
        if result.returncode:
            raise BinError(response['error'])
        return response['registry']

    def read(self, password=None):
        return self._call({'operation': 'read'}, password=password)

    def save(self, key, expected, entry, directory, artifact=None, password=None):
        return self._call({'operation': 'entry', 'key': key, 'expected': expected, 'entry': entry,
                           'directory': str(directory), 'artifact': artifact is not None,
                           'mode': stat.S_IMODE(artifact.stat().st_mode) if artifact is not None else None},
                          artifact, password)

    def migrate(self, directory, password=None):
        return self._call({'operation': 'migrate', 'directory': str(directory)}, password=password)
