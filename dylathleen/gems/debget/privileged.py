#!/usr/bin/python3
"""The installed, root-owned boundary for deb-get and its APT subprocesses."""
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import urlsplit

NATIVE = '/usr/bin/deb-get'
AUDITED_SHA256 = '12d59a3c463a4bd07576dca3b4e15f8258f929c736fed45e72dd1496ae0f1187'
GUARD = Path('/usr/lib/dylathleen/debget-guard')
GUARDED = ('apt-get', 'apt', 'aptitude', 'wget', 'apt-hook')
ROOT = Path('/etc/deb-get')
CACHE = Path('/var/cache/deb-get')
PKG = re.compile(r'[a-z0-9][a-z0-9+.-]+\Z')
METHODS = {'apt', 'ppa', 'direct', 'github', 'gitlab', 'website'}
MARKER = 'DYLATHLEEN_DEBGET '
LIMIT = 256 * 1024
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C',
       'HOME': '/root', 'DISABLE_APT': 'y'}


class DebGetError(Exception):
    pass


def package(value):
    if not isinstance(value, str) or len(value) > 255 or not PKG.fullmatch(value):
        raise DebGetError('Invalid deb-get package name.')
    return value


@lru_cache(maxsize=1)
def root_group_is_private():
    try:
        members = grp.getgrgid(0).gr_mem
        return (all(pwd.getpwnam(member).pw_uid == 0 for member in members)
                and all(user.pw_uid == 0 or user.pw_gid != 0 for user in pwd.getpwall()))
    except KeyError:
        return False


def secure(path, directory=False, missing=False, downloaded_recipe=False):
    """Require root-controlled paths without symlinks for privileged operations."""
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                return False
            raise
        check = stat.S_ISDIR if part != path or directory else stat.S_ISREG
        if not check(info.st_mode) or info.st_uid != 0 or info.st_mode & stat.S_IWOTH:
            raise DebGetError(f'Unsafe system path: {part}')
        if info.st_mode & stat.S_IWGRP:
            # GitHub tarballs store ordinary recipes as root:root 0664.
            # Only the downloaded file may use that mode. Its parents and
            # locally edited recipes still require owner-only write access.
            root_only = (downloaded_recipe and part == path and not directory and info.st_gid == 0
                         and root_group_is_private()
                         and 'system.posix_acl_access' not in os.listxattr(part, follow_symlinks=False))
            if not root_only:
                raise DebGetError(f'Unsafe system path: {part}')
    return True


def registered(root=ROOT):
    path = root / 'installed'
    try:
        content = path.read_text()
    except FileNotFoundError:
        return {}
    result = {}
    for line in content.splitlines():
        fields = line.split()
        if len(fields) == 3 and PKG.fullmatch(fields[0]) and fields[1].isdigit() and fields[2] in METHODS:
            result[fields[0]] = fields[2]
    return result


def emit(event, **fields):
    print(MARKER + json.dumps({'event': event, **fields}), flush=True)


def ask(event, **fields):
    emit(event, **fields)
    control = os.environ.get('DG_CONTROL')
    if control:
        # xargs gives apt-get /dev/null on stdin during upgrade --dg-only.
        with open(control, 'r') as stream:
            answer = stream.readline()
    else:
        answer = sys.stdin.readline()
    if answer != 'yes\n':
        raise DebGetError('Transaction cancelled.')


def recipe_text(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
        raise DebGetError(f'Invalid recipe file: {path}')
    with path.open('r', encoding='utf-8') as stream:
        return stream.read(LIMIT + 1)


def hash_text(source):
    return hashlib.sha256(source.encode()).hexdigest()


def atomic_file(path, source, mode=0o644):
    with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def change_recipe(request):
    name = package(request['name'])
    directory = ROOT / '99-local.d'
    backup_dir = ROOT / 'dylathleen-backups'
    for path in (ROOT, directory, backup_dir, backup_dir / name):
        if not secure(path, directory=True, missing=True):
            path.mkdir(mode=0o755 if path in (ROOT, directory) else 0o700, exist_ok=True)
            secure(path, directory=True)
    current = directory / name
    previous = recipe_text(current) if secure(current, missing=True) else None
    if (hash_text(previous) if previous is not None else None) != request['expected']:
        raise DebGetError('Recipe changed since it was opened. Reload before saving.')
    action = request['operation']
    if action == 'restore':
        revision = request['revision']
        if not re.fullmatch(r'[0-9a-f]{64}', revision):
            raise DebGetError('Invalid backup revision.')
        saved = backup_dir / name / revision
        secure(saved)
        source = recipe_text(saved)
    elif action == 'save':
        source = request['source']
    elif action == 'delete':
        source = None
    else:
        raise DebGetError('Invalid recipe operation.')
    if source is not None:
        if len(source.encode()) > LIMIT or '\0' in source:
            raise DebGetError('Invalid recipe contents.')
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-n'], input=source,
                                env=ENV, text=True, capture_output=True, timeout=15)
        if result.returncode:
            raise DebGetError(result.stderr.strip())
    if previous is not None:
        saved = backup_dir / name / hash_text(previous)
        if not secure(saved, missing=True):
            atomic_file(saved, previous, 0o644)
    if source is None:
        current.unlink(missing_ok=True)
    else:
        atomic_file(current, source)


def backup_list(request):
    name = package(request['name'])
    path = ROOT / 'dylathleen-backups' / name
    choices = []
    if secure(path, directory=True, missing=True):
        choices = [p.name for p in sorted(path.iterdir(), key=lambda item: item.lstat().st_mtime, reverse=True)
                   if re.fullmatch(r'[0-9a-f]{64}', p.name) and secure(p)]
    emit('backups', choices=choices)


def target(arg, allowed):
    if arg in allowed:
        return arg
    path = Path(arg)
    if path.parent != CACHE or not path.name.endswith('.deb'):
        raise DebGetError('APT target is outside the authorized deb-get packages.')
    secure(path)
    name = subprocess.check_output(['/usr/bin/dpkg-deb', '-f', arg, 'Package'], env=ENV, text=True, timeout=20).strip()
    if name not in allowed:
        raise DebGetError(f'Unexpected package in downloaded DEB: {name}')
    return name


def apt_plan(args, allowed, operation):
    """Return a canonical argument vector for the audited upstream call shapes."""
    if not args:
        raise DebGetError('Missing APT arguments.')
    if operation in {'update', 'catalogue', 'show'}:
        raise DebGetError('APT is disabled while reading recipes and checking updates.')
    if args[0] == 'update':
        prefix = 'Dir::Etc::sourcelist=sources.list.d/'
        if (len(args) != 7 or args[1::2] != ['-o', '-o', '-o'] or not args[2].startswith(prefix)
                or args[4:] != ['Dir::Etc::sourceparts=-', '-o', 'APT::Get::List-Cleanup=0']
                or operation not in {'install', 'reinstall', 'upgrade-one', 'upgrade'}):
            raise DebGetError('A system-wide APT index refresh is forbidden.')
        name = args[2][len(prefix):]
        if not re.fullmatch(r'[a-zA-Z0-9_.+-]+\.list', name):
            raise DebGetError('Invalid recipe APT source name.')
        secure(Path('/etc/apt/sources.list.d') / name)
        return [*args, '-o', 'APT::Update::Error-Mode=any'], set()
    flags = {'-q', '-q=2', '-y', '--autoremove', '--only-upgrade', '--reinstall', '--allow-downgrades'}
    cmd = None
    parameters = []
    seen = set()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '-o' and args[i + 1:i + 2] == ['Dpkg::Progress-Fancy=1']:
            i += 1
        elif arg in flags:
            seen.add(arg)
        elif cmd is None and arg in {'install', 'remove', 'purge'}:
            cmd = arg
        elif cmd and not arg.startswith('-'):
            parameters.append(arg)
        else:
            raise DebGetError('Disallowed APT command or option.')
        i += 1
    expected = {'remove': {'remove', 'purge'}, 'purge': {'purge'}}.get(operation, {'install'})
    if cmd not in expected:
        raise DebGetError('APT action does not match the authorized transaction.')
    if parameters in ([], ['']) and operation == 'upgrade' and '--only-upgrade' in seen:
        return [], set()  # upstream xargs invokes apt-get even with no registered APT packages
    if not parameters:
        raise DebGetError('APT requires explicit package targets.')
    names = {target(arg, allowed) for arg in parameters}
    if cmd != 'install' and any(arg not in names for arg in parameters):
        raise DebGetError('Invalid removal target.')
    options = []
    if operation == 'upgrade-one':
        options.append('--only-upgrade')
    elif operation == 'upgrade' and '--only-upgrade' in seen:
        options.append('--only-upgrade')
    elif operation == 'reinstall':
        options.extend(arg for arg in ('--reinstall', '--allow-downgrades') if arg in seen)
    return [cmd, *options, *parameters], names


def apt(args):
    allowed = set(json.loads(os.environ['DG_ALLOWED']))
    operation = os.environ['DG_ACTION']
    command, targets = apt_plan(args, allowed, operation)
    if not command:
        return 0
    env = {**ENV, 'DEBIAN_FRONTEND': 'noninteractive'}
    apt_get = '/usr/bin/apt-get'
    if command[0] == 'update':
        return subprocess.call([apt_get, *command], env=env, stdin=subprocess.DEVNULL)
    options = ['-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold',
               '-o', 'APT::Get::AutomaticRemove=false', '-o', 'DPkg::Lock::Timeout=20',
               '-o', 'Acquire::http::Timeout=30', '-o', 'Acquire::https::Timeout=30']
    plan = subprocess.run([apt_get, '-s', *options, *command], env=env, stdin=subprocess.DEVNULL,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    if plan.returncode:
        raise DebGetError(plan.stdout.strip())
    changes = [line for line in plan.stdout.splitlines() if line.startswith(('Inst ', 'Remv ', 'Purg ', 'Conf '))]
    removals = {line.split()[1].split(':')[0] for line in changes if line.startswith(('Remv ', 'Purg '))}
    removable = targets if command[0] in {'remove', 'purge'} else set()
    if removals - removable:
        raise DebGetError('APT would remove packages outside the authorized operation.')
    if command[0] == 'install':
        options.append('--no-remove')
    hook = os.environ.get('DG_APT_HOOK')
    if not hook:
        raise DebGetError('Missing APT transaction hook.')
    secure(Path(hook))
    if not os.access(hook, os.X_OK):
        raise DebGetError('APT transaction hook is not executable.')
    options.extend(['-o', f'DPkg::Pre-Install-Pkgs::={hook}',
                    '-o', f'DPkg::Tools::Options::{hook}::Version=2'])
    env['DG_REMOVABLE'] = json.dumps(sorted(removable))
    env['DG_HELPER'] = os.environ['DG_HELPER']
    ask('transaction', plan='\n'.join(changes) or plan.stdout.strip())
    emit('apt-running')
    try:
        code = subprocess.call([apt_get, '-y', *options, *command], env=env, stdin=subprocess.DEVNULL)
        if code:
            raise DebGetError(f'APT failed with exit status {code}.')
        return 0
    finally:
        emit('apt-finished')


def check_apt_hook(lines, removable):
    if next(lines, '').strip() != 'VERSION 2':
        raise DebGetError('Unexpected APT hook protocol.')
    for line in lines:
        if not line.strip():
            break
    for line in lines:
        parts = line.split(maxsplit=4)
        if len(parts) == 5 and parts[-1].strip() == '**REMOVE**' and parts[0] not in removable:
            raise DebGetError('APT changed its plan and would remove a package outside the authorized operation.')


def wget(args):
    args = [arg for arg in args if arg]
    config = os.environ.get('DG_WGET_CONFIG')
    urls = [urlsplit(arg) for arg in args if '://' in arg]
    if config and len(urls) == 1 and (urls[0].scheme, urls[0].netloc) == ('https', 'api.github.com'):
        args = ['--config=' + config, '--max-redirect=0', *args]
    return subprocess.call(['/usr/bin/wget', *args], env=ENV)


def verify_native():
    secure(Path(NATIVE))
    if hashlib.sha256(Path(NATIVE).read_bytes()).hexdigest() != AUDITED_SHA256:
        raise DebGetError('The installed deb-get script differs from the audited 0.4.7 package.')


def install_guards(folder):
    secure(GUARD)
    if not os.access(GUARD, os.X_OK):
        raise DebGetError('The installed deb-get guard cannot execute.')
    # The native script gets one private search path. Removal, EACCES or
    # ENOEXEC of any guard cannot reveal another apt-get further down PATH.
    for name in GUARDED:
        path = folder / name
        shutil.copyfile(GUARD, path)
        path.chmod(0o700)
    for directory in ('/usr/sbin', '/usr/bin'):
        for tool in Path(directory).iterdir():
            path = folder / tool.name
            if tool.name not in GUARDED and not path.is_symlink() and not path.exists():
                path.symlink_to(tool)
    env = {**ENV, 'PATH': str(folder), 'DG_HELPER': str(Path(__file__).resolve())}
    for name in GUARDED:
        probe = subprocess.run([str(folder / name), '--dylathleen-probe'], env=env, capture_output=True,
                               stdin=subprocess.DEVNULL, text=True, timeout=10)
        if probe.returncode or probe.stdout.strip() != name:
            raise DebGetError('Failed to verify deb-get transaction guard: ' + name)


def native_request(request):
    action = request['action']
    names = [package(name) for name in request.get('packages', [])]
    check_native_paths()
    state = registered()
    if action in {'update', 'catalogue', 'upgrade'}:
        if names:
            raise DebGetError('Unexpected deb-get package arguments.')
    elif action in {'install', 'reinstall', 'upgrade-one', 'remove', 'purge', 'show'}:
        if len(names) != 1:
            raise DebGetError('Choose exactly one deb-get recipe.')
        if action in {'reinstall', 'upgrade-one', 'remove', 'purge'} and names[0] not in state:
            raise DebGetError('The package is absent from the deb-get registry.')
        if not has_recipe(names[0], include_disabled=action in {'remove', 'purge'}):
            raise DebGetError('The recipe no longer exists in the deb-get catalogue.')
    else:
        raise DebGetError('Unsupported deb-get command.')
    verify_native()
    if (ROOT / 'installed').exists():
        secure(ROOT / 'installed')
    # Bypass PATH wrappers, so options and credentials are supplied exactly once.
    try:
        native_version = subprocess.check_output([NATIVE, 'version'], env=ENV, text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise DebGetError('Could not determine the native deb-get version.') from error
    if not re.search(r'^0\.4\.7$', native_version, re.M):
        raise DebGetError('Privileged operation requires deb-get 0.4.7, the audited version.')
    arguments = {'catalogue': ['update', '--repos-only'], 'upgrade': ['upgrade', '--dg-only'],
                 'upgrade-one': ['reinstall']}.get(action, [action])
    allowed = set(names) if names else set(state)
    token = request.get('token', '')
    if token and not re.fullmatch(r'[a-zA-Z0-9_-]{1,512}', token):
        raise DebGetError('Invalid DEBGET_TOKEN format.')
    # /run can be mounted noexec. Keep a private copy of the executable guards
    # on the package filesystem so a self-update or removal cannot replace them.
    guard_root = Path('/usr/lib/dylathleen')
    secure(guard_root, directory=True)
    with TemporaryDirectory(prefix='.debget-', dir=guard_root) as directory:
        folder = Path(directory)
        if token:
            secret = folder / 'wgetrc'
            atomic_file(secret, f'header = Authorization: token {token}\n', 0o600)
        tools = folder / 'bin'
        tools.mkdir(mode=0o700)
        install_guards(tools)
        env = {**ENV, 'PATH': str(tools), 'DG_ALLOWED': json.dumps(sorted(allowed)),
               'DG_ACTION': action, 'DG_HELPER': str(Path(__file__).resolve()), 'DG_WGET_CONFIG': str(folder / 'wgetrc') if token else '',
               'DG_FAILURE_FILE': str(folder / 'failed'), 'DG_CONTROL': f'/proc/{os.getpid()}/fd/0',
               'DG_APT_HOOK': str(tools / 'apt-hook')}
        if action in {'update', 'catalogue', 'show'}:
            env['DEBGET_CACHE_RTN'] = '0' if action in {'update', 'catalogue'} else '60'
        if action == 'reinstall' and state.get(names[0]) in {'github', 'gitlab', 'website'}:
            code = run_native(['show', *names], {**env, 'DG_ACTION': 'show'})
            if code or (folder / 'failed').exists():
                return code or 1
        code = run_native([*arguments, *names], env)
        return code or int((folder / 'failed').exists())


def check_native_paths():
    if secure(ROOT, directory=True, missing=True):
        for path in ROOT.iterdir():
            if path.name == 'installed' or path.suffix == '.repo':
                secure(path)
            if path.suffix == '.d':
                secure(path, directory=True)
                manifest = ROOT / (path.stem + '.repo')
                downloaded = path.stem not in {'00-builtin', '99-local'} and secure(manifest, missing=True)
                for recipe in path.iterdir():
                    if PKG.fullmatch(recipe.name):
                        secure(recipe, downloaded_recipe=downloaded)
    if secure(CACHE, directory=True, missing=True):
        for path in CACHE.iterdir():
            if path.is_symlink():
                raise DebGetError(f'Unsafe cache link: {path}')


def has_recipe(name, include_disabled=False):
    if name == 'deb-get' or (ROOT / '99-local.d' / name).is_file():
        return True
    for manifest in ROOT.glob('*.repo'):
        if manifest.stem in {'00-builtin', '99-local'}:
            continue
        records = recipe_text(manifest).splitlines()[1:]
        if name in records or include_disabled and '#' + name in records:
            if (ROOT / (manifest.stem + '.d') / name).is_file():
                return True
    return False


def run_native(args, env):
    launcher = 'export DG_NATIVE_PID=$$; exec "$@"'
    with subprocess.Popen(['/bin/bash', '-c', launcher, 'deb-get', NATIVE, *args],
                          env=env, start_new_session=True) as proc:
        def forward(signum, _frame):
            try:
                os.killpg(proc.pid, signum)
            except ProcessLookupError:
                pass

        previous = signal.signal(signal.SIGTERM, forward)
        try:
            return proc.wait()
        finally:
            signal.signal(signal.SIGTERM, previous)


def main():
    if os.geteuid() != 0:
        raise DebGetError('This helper requires administrator privileges.')
    if len(sys.argv) > 1:
        if sys.argv[1] in GUARDED and sys.argv[2:] == ['--dylathleen-probe']:
            print(sys.argv[1])
            return 0
        if sys.argv[1] in {'apt-get', 'apt'}:
            return apt(sys.argv[2:])
        if sys.argv[1] == 'wget':
            return wget(sys.argv[2:])
        if sys.argv[1] == 'apt-hook':
            check_apt_hook(iter(sys.stdin), set(json.loads(os.environ['DG_REMOVABLE'])))
            return 0
        raise DebGetError('This package manager command is forbidden.')
    line = sys.stdin.buffer.readline(6 * LIMIT)
    request = json.loads(line)
    lockdir = Path('/run')
    secure(lockdir, directory=True)
    descriptor = os.open(lockdir / 'dylathleen-debget.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDONLY, 0o600)
    with os.fdopen(descriptor, 'rb') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise DebGetError('Unsafe deb-get operation lock.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DebGetError('Another Dylath-Leen deb-get operation is running.') from error
        if request['operation'] == 'native':
            return native_request(request)
        if request['operation'] == 'backups':
            backup_list(request)
        else:
            change_recipe(request)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (DebGetError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        emit('error', message=str(error))
        if len(sys.argv) > 1 and sys.argv[1] in {'apt-get', 'apt', 'aptitude'}:
            failure = os.environ.get('DG_FAILURE_FILE')
            if failure:
                Path(failure).touch()
            if os.environ.get('DG_ACTION') in {'remove', 'purge'}:
                # Upstream 0.4.7 drops the registry entry even on APT failure.
                # Stop the waiting script before it reaches remove_installed.
                pid = os.environ.get('DG_NATIVE_PID')
                if pid and pid.isdecimal():
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        sys.exit(1)
