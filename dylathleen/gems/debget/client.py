import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from dylathleen.commons.system import password_input
from dylathleen.gems.debget.privileged import DebGetError, LIMIT, MARKER, NATIVE, secure

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


def _authorize(password):
    if not shutil.which('sudo'):
        raise DebGetError('sudo is required for deb-get operations.')
    if password is None:
        return subprocess.run(['sudo', '-n', '-v'], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=15).returncode == 0
    with password_input(password) as stdin:
        return subprocess.run(['sudo', '-S', '-v'], stdin=stdin, capture_output=True, timeout=30).returncode == 0


def invoke(request, watcher, confirm, password=None):
    helper = str(Path(__file__).with_name('privileged.py').resolve())
    if os.geteuid() == 0:
        raise DebGetError('Run Dylath-Leen as your regular user.')
    secure(Path(helper))  # Only elevate the installed, root-owned code.
    if not _authorize(password):
        valid, password = watcher.request_root_password()
        if not valid:
            raise Cancelled()
        if not _authorize(password):
            raise DebGetError('Administrator authorization failed.')

    token = user_token() if request['operation'] == 'native' else ''
    request = {**request, 'token': token}
    payload = json.dumps(request)
    if len(payload) >= 6 * LIMIT:
        raise DebGetError('Recipe or operation is too large.')
    command = ['sudo', '-n', '-H', '--', '/usr/bin/python3', '-I', helper]
    events, lines = [], []
    queue = Queue()

    def read_output(stream):
        try:
            for line in stream:
                queue.put(line)
        finally:
            queue.put(None)

    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors='replace') as proc:
        Thread(target=read_output, args=(proc.stdout,), daemon=True).start()
        proc.stdin.write(payload + '\n')
        proc.stdin.flush()
        waiting_from = time.monotonic()
        apt_running = False
        approved_mutation = False
        cancelled = False
        stopped = False
        while True:
            try:
                line = queue.get(timeout=0.25)
            except Empty:
                if not apt_running and (watcher.should_stop() or time.monotonic() - waiting_from > 180):
                    stopped = True
                    proc.terminate()
                    break
                continue
            if line is None:
                break
            line = ESCAPES.sub('', line).strip()
            if token:
                line = line.replace(token, '[redacted]')
            waiting_from = time.monotonic()
            if line.startswith(MARKER):
                try:
                    event = json.loads(line[len(MARKER):])
                except ValueError:
                    continue
                events.append(event)
                if event['event'] == 'apt-running':
                    apt_running = True
                elif event['event'] == 'apt-finished':
                    apt_running = False
                elif event['event'] == 'transaction':
                    approved = confirm(event['plan'])
                    approved_mutation |= approved
                    proc.stdin.write('yes\n' if approved else 'no\n')
                    proc.stdin.flush()
                    cancelled |= not approved
            else:
                if line:
                    lines.append(line)
                    watcher.print(line)
                    progress = re.search(r'\b(\d{1,3})%(?:\s|$)', line)
                    if progress:
                        watcher.change_progress(min(99, int(progress[1])))
                if EULA.search(line):
                    approved = confirm('\n'.join(lines[-80:]))
                    proc.stdin.write('1\n' if approved else '2\n')
                    proc.stdin.flush()
                    cancelled |= not approved
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if stopped:
            raise DebGetError('deb-get timed out or the operation was stopped.')
        if cancelled and not approved_mutation:
            raise Cancelled()
        errors = [event['message'] for event in events if event['event'] == 'error']
        if proc.returncode or errors:
            raise DebGetError('\n'.join(errors) or '\n'.join(lines[-12:]) or f'deb-get exit status {proc.returncode}')
    return lines, events


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
