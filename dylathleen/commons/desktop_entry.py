"""Read desktop launchers without evaluating a shell command."""
import configparser
import locale
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_ESCAPE = {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}
_ENV_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=.*', re.S)


def unescape(value):
    return re.sub(r'\\([sntr\\])', lambda match: _ESCAPE[match[1]], value)


def executable_path(value):
    if not value or '\0' in value:
        return None
    if os.path.isabs(value):
        return os.path.normpath(value) if os.path.isfile(value) and os.access(value, os.X_OK) else None
    if '/' not in value:
        found = shutil.which(value)
        if found:
            return os.path.abspath(found)
    return None


def path_identity(path):
    # Resolve directory aliases such as /bin on usr-merged systems while
    # preserving the name of a symlink used as a multicall executable.
    directory, name = os.path.split(path)
    return os.path.join(os.path.realpath(directory), name)


def _tokens(line):
    parts = []
    quoted = escaped = started = False
    for char in unescape(line):
        if escaped:
            parts.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if started:
                yield ''.join(parts)
                parts.clear()
                started = False
            continue
        else:
            parts.append(char)
        started = True
    if quoted or escaped:
        raise ValueError('Incomplete desktop entry quoting')
    if started:
        yield ''.join(parts)


def _argv(line, name, icon, path):
    result = []
    for token in _tokens(line):
        if token == '%i':
            if icon:
                result.extend(('--icon', icon))
            continue
        values = {'%': '%', 'c': name, 'k': str(path)}
        parts = []
        index = 0
        omit = False
        while index < len(token):
            char = token[index]
            index += 1
            if char == '%':
                if index == len(token):
                    raise ValueError('Incomplete desktop entry field code')
                code = token[index]
                index += 1
                if code in 'fFuUdDnNvm':
                    omit = True
                elif code in values:
                    parts.append(values[code])
                else:
                    raise ValueError('Unsupported desktop entry field code')
            else:
                parts.append(char)
        if not omit:
            result.append(''.join(parts))
    if not result or '=' in result[0] or not result[0] or any('\0' in item for item in result):
        raise ValueError('Invalid desktop entry program')
    return tuple(result)


def _name(entry, path):
    language = locale.getlocale(locale.LC_MESSAGES)[0]
    if language:
        for variant in (language, language.split('_')[0]):
            if entry.get(f'Name[{variant}]'):
                return unescape(entry[f'Name[{variant}]'])
    return unescape(entry.get('Name', path.stem))


@dataclass(frozen=True)
class LaunchCommand:
    argv: tuple[str, ...]
    terminal: bool
    executable: str
    desktop: str = ''
    cwd: str | None = None


@dataclass(frozen=True)
class DesktopEntry:
    path: Path
    name: str
    argv: tuple[str, ...]
    try_exec: str
    terminal: bool
    cwd: str | None

    @classmethod
    def read(cls, path):
        parser = configparser.ConfigParser(interpolation=None, delimiters=('=',), strict=False)
        parser.optionxform = str
        try:
            if not path.is_file() or path.stat().st_size > 256 * 1024:
                return None
            parser.read_string(path.read_text(encoding='utf-8'))
            entry = parser['Desktop Entry']
            if entry.get('Type') != 'Application' or any(entry.get(key) == 'true' for key in ('Hidden', 'NoDisplay')):
                return None
            name = _name(entry, path)
            return cls(path, name, _argv(entry['Exec'], name, unescape(entry.get('Icon', '')), path),
                       unescape(entry.get('TryExec', '')), entry.get('Terminal') == 'true',
                       unescape(entry.get('Path', '')) or None)
        except (OSError, UnicodeError, configparser.Error, KeyError, ValueError):
            return None

    def command(self, aliases):
        def resolve(value):
            if os.path.isabs(value):
                value = aliases.get(path_identity(value), value)
            return executable_path(value)

        if self.try_exec and not resolve(self.try_exec):
            return None
        launcher = resolve(self.argv[0])
        if not launcher:
            return None
        argv = [launcher, *self.argv[1:]]
        executable = launcher
        # A regular env assignment prefix is common in desktop entries.
        # Other wrappers stay opaque and are never matched by their arguments.
        if path_identity(launcher) == path_identity('/usr/bin/env'):
            index = 1
            while index < len(argv) and _ENV_ASSIGNMENT.fullmatch(argv[index]):
                index += 1
            if index < len(argv) and argv[index] == '--':
                index += 1
            if index < len(argv) and not argv[index].startswith('-'):
                executable = resolve(argv[index])
                if not executable:
                    return None
                argv[index] = executable
        if self.cwd and (not os.path.isabs(self.cwd) or not os.path.isdir(self.cwd)):
            return None
        return LaunchCommand(tuple(argv), self.terminal, executable, str(self.path), self.cwd)


def application_dirs():
    user_dir = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local/share')
    system_dirs = (os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':')
    roots = dict.fromkeys(Path(root) / 'applications' for root in (user_dir, *system_dirs)
                          if os.path.isabs(root))
    return tuple(roots)


class DesktopCatalogue:
    def __init__(self, roots=None):
        self.roots = application_dirs() if roots is None else roots
        self.entries = {}
        for root in self.roots:
            for path in sorted(root.rglob('*.desktop')):
                desktop_id = '-'.join(path.relative_to(root).parts)
                if desktop_id not in self.entries:
                    # An invalid/hidden high-priority file still shadows the ID.
                    self.entries[desktop_id] = DesktopEntry.read(path)

    def _owned_entries(self, files):
        for path in files:
            path = Path(path)
            if path.suffix != '.desktop':
                continue
            for root in self.roots:
                if path.is_relative_to(root):
                    yield self.entries.get('-'.join(path.relative_to(root).parts))
                    break
            else:
                yield DesktopEntry.read(path)

    def matching(self, files, executables, managed_path=None, previous_paths=()):
        owned_entries = {entry.path: entry for entry in self._owned_entries(files) if entry}
        owned_executables = {path_identity(path) for path in executables}
        aliases = {path_identity(old): managed_path for old in previous_paths
                   if managed_path and os.path.isabs(old)}
        owned_executables.update(aliases)
        entries = {entry.path: entry for entry in self.entries.values() if entry}
        entries.update(owned_entries)
        if managed_path:
            sidecar = Path(managed_path).with_suffix('.desktop')
            if sidecar not in entries:
                entry = DesktopEntry.read(sidecar)
                if entry:
                    entries[sidecar] = entry
        choices = []
        for entry in entries.values():
            command = entry.command(aliases)
            if command and (entry.path in owned_entries or path_identity(command.executable) in owned_executables):
                choices.append((entry.name, command))
        return tuple(sorted(choices, key=lambda item: (item[0].casefold(), item[1].desktop)))
