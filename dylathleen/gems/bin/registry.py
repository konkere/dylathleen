"""Local filesystem transactions for bin, also used as the isolated sudo helper."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory


class BinError(Exception):
    pass


def config_path():
    home = Path.home()
    legacy = home / '.bin/config.json'
    override = os.environ.get('BIN_CONFIG')
    if override:
        return Path(override).absolute()
    if legacy.exists():
        return legacy
    xdg = os.environ.get('XDG_CONFIG_HOME')
    if xdg and Path(xdg).is_dir():
        return Path(xdg).absolute() / 'bin/config.json'
    if (home / '.config').is_dir():
        return home / '.config/bin/config.json'
    return legacy


def empty_registry():
    return {'bins': {}}


def read_registry(path):
    try:
        data = path.read_text()
    except FileNotFoundError:
        return empty_registry()
    result = json.loads(data) if data.strip() else empty_registry()
    if not isinstance(result, dict) or (result.get('bins') is not None and not isinstance(result['bins'], dict)):
        raise BinError(f'Invalid bin registry: {path}')
    result['bins'] = result.get('bins') or {}
    for key, entry in result['bins'].items():
        if not isinstance(entry, dict) or entry.get('path') != key:
            raise BinError(f'Invalid bin registry entry: {key}')
    return result


def expanded_path(value):
    # bin expands variables in legacy registry entries with Go's os.ExpandEnv.
    value = re.sub(r'\$(?:\{([^}]+)\}|([a-zA-Z_][a-zA-Z_0-9]*))',
                   lambda match: os.environ.get(match[1] or match[2], ''), value)
    return absolute_path(value)


def absolute_path(value):
    path = Path(value)
    if (not path.is_absolute() or len(path.parts) < 2 or '..' in path.parts
            or any(char in value for char in ('\n', '\r', '\0', '$'))):
        raise BinError(f'Invalid absolute path: {value}')
    return path


def directory(path, create=False, system=False):
    for part in [*reversed(path.parents), path]:
        if create:
            part.mkdir(exist_ok=True)
        info = part.stat() if not system else part.lstat()
        if not stat.S_ISDIR(info.st_mode) or (system and (info.st_uid != 0 or info.st_mode & 0o022)):
            raise BinError(f'Unsafe directory: {part}')


def regular_file(path, system=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or (system and (info.st_uid != 0 or info.st_mode & 0o022)):
        raise BinError(f'Unsafe file: {path}')
    return info


def digest(stream):
    checksum = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        checksum.update(block)
    return checksum.hexdigest()


def check_registered_file(path, entry, system):
    info = regular_file(path, system)
    if info and entry.get('hash'):
        with path.open('rb') as stream:
            if digest(stream) != entry['hash']:
                raise BinError(f'File differs from the registered binary: {path}')
    return info


def write_json(path, value):
    with NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=4)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def registry_lock(path, system):
    directory(path.parent, create=True, system=system)
    regular_file(path, system)
    descriptor = os.open(path.with_name(path.name + '.dylathleen.lock'),
                         os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY, 0o600)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise BinError(f'Unsafe registry lock: {path}')
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def check_registry_unchanged(path, previous):
    if read_registry(path) != previous:
        raise BinError('The bin registry changed during the operation. Refresh and retry.')


def backup_file(path, cleanup, rollback):
    folder = cleanup.enter_context(TemporaryDirectory(dir=path.parent, prefix='.dylathleen-bin-'))
    backup = Path(folder) / path.name
    os.link(path, backup, follow_symlinks=False)
    rollback.callback(os.replace, backup, path)


def publish_file(stream, target, mode, expected_hash, replace=False):
    with NamedTemporaryFile('w+b', dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            shutil.copyfileobj(stream, output)
            output.flush()
            if expected_hash:
                output.seek(0)
                if digest(output) != expected_hash:
                    raise BinError(f'Binary checksum mismatch: {target}')
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
            if replace:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def save_entry(path, previous, request, artifact, system):
    key, expected, entry = (request[field] for field in ('key', 'expected', 'entry'))
    bins = previous['bins']
    if bins.get(key) != expected:
        raise BinError('The bin registry changed during the operation. Refresh and retry.')
    target = expanded_path(key)
    replacement_key = key if artifact is None else str(target)
    if replacement_key != key and replacement_key in bins:
        raise BinError(f'Path is already registered: {target}')
    if expected is None and any(expanded_path(other) == target for other in bins):
        raise BinError(f'Path is already registered: {target}')

    updated = {**previous, 'default_path': previous.get('default_path') or request['directory'],
               'bins': dict(bins)}
    updated['bins'].pop(key, None)
    if entry is not None:
        updated['bins'][replacement_key] = {**entry, 'path': replacement_key}

    with ExitStack() as cleanup, ExitStack() as rollback:
        if artifact is not None or entry is None:
            try:
                directory(target.parent, create=artifact is not None, system=system)
            except FileNotFoundError:
                if artifact is not None:
                    raise
                info = None
            else:
                info = check_registered_file(target, expected or {}, system)
            if not expected and info:
                raise BinError(f'Destination already exists: {target}')
            if info:
                backup_file(target, cleanup, rollback)
            if artifact is not None:
                mode = 0o755 if system else request['mode'] & 0o777 | 0o100
                publish_file(artifact, target, mode, entry['hash'], replace=info is not None)
                if not info:
                    rollback.callback(target.unlink)
            elif info:
                target.unlink()

        check_registry_unchanged(path, previous)
        write_json(path, updated)
        rollback.pop_all()
    return updated


def migrate(path, previous, destination, system):
    target_dir = absolute_path(destination)
    directory(target_dir, create=True, system=system)
    moves = []
    updated = {**previous, 'bins': {}}
    for key, entry in previous['bins'].items():
        source = expanded_path(key)
        target = target_dir / source.name
        if str(target) in updated['bins']:
            raise BinError(f'Conflicting registered names: {target}')
        updated['bins'][str(target)] = {**entry, 'path': str(target)}
        if source == target:
            continue
        try:
            directory(source.parent, system=system)
        except FileNotFoundError:
            info = None
        else:
            info = check_registered_file(source, entry, system)
        if os.path.lexists(target):
            raise BinError(f'Destination already exists: {target}')
        if info:
            moves.append((source, target, info.st_mode & 0o777, entry.get('hash')))

    updated['default_path'] = str(target_dir)

    with ExitStack() as remove_copies:
        for source, target, mode, checksum in moves:
            with source.open('rb') as stream:
                publish_file(stream, target, mode, checksum)
            remove_copies.callback(target.unlink)
        check_registry_unchanged(path, previous)
        # Keep the old binaries in place until the registry points to working copies.
        write_json(path, updated)
        with ExitStack() as cleanup:
            restore_sources = ExitStack()
            try:
                for source, _, _, _ in moves:
                    backup_file(source, cleanup, restore_sources)
                    source.unlink()
            except Exception:
                try:
                    restore_sources.close()
                    write_json(path, previous)
                except Exception:
                    # The active registry still points to the destination.
                    remove_copies.pop_all()
                    raise
                raise
            restore_sources.pop_all()
        remove_copies.pop_all()
    return updated


def transact(path, request, artifact=None, system=False):
    if request['operation'] == 'read':
        if system:
            try:
                directory(path.parent, system=True)
            except FileNotFoundError:
                return empty_registry()
            regular_file(path, system=True)
        return read_registry(path)
    with registry_lock(path, system):
        previous = read_registry(path)
        if request['operation'] == 'migrate':
            return migrate(path, previous, request['directory'], system)
        if request['operation'] == 'entry':
            return save_entry(path, previous, request, artifact, system)
        raise BinError('Unknown registry operation')


def main():
    if os.geteuid() != 0:
        raise BinError('The system registry helper requires root')
    request = json.loads(sys.stdin.buffer.readline())
    artifact = sys.stdin.buffer if request.get('artifact') else None
    result = transact(config_path(), request, artifact, system=True)
    json.dump({'registry': result}, sys.stdout)


if __name__ == '__main__':
    try:
        main()
    except (BinError, OSError, ValueError) as error:
        json.dump({'error': str(error)}, sys.stdout)
        sys.exit(1)
