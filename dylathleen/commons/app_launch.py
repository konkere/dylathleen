"""Shared launch profiles and desktop command resolution for installed packages."""
import json
import os
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock, Thread

from dylathleen.api.abstract.model import CustomSoftwareAction
from dylathleen.api.abstract.view import FormComponent, InputOption, MessageType, SelectViewType, SingleSelectComponent, TextInputComponent
from dylathleen.api.paths import CONFIG_DIR
from dylathleen.commons.desktop_entry import LaunchCommand, executable_path, path_identity
from dylathleen.commons.installed_files import InstalledFiles

_MODES = ('auto', 'gui', 'terminal', 'disabled')
_LOCK = RLock()


@dataclass(frozen=True)
class LaunchProfile:
    mode: str = 'auto'
    path: str = ''
    arguments: tuple[str, ...] | None = None
    desktop: str = ''
    previous_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchState:
    command: LaunchCommand | None = None
    reason: str | None = 'launch.unknown'


class LaunchProfiles:
    def __init__(self, path=None):
        self.path = Path(CONFIG_DIR) / 'launch.json' if path is None else Path(path)

    def read(self):
        with _LOCK:
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                return {}
        if not isinstance(data, dict):
            raise ValueError('Invalid launch profiles')
        return data

    @staticmethod
    def profile(data, key):
        fields = data.get(key, {})
        if not isinstance(fields, dict) or fields.get('mode', 'auto') not in _MODES:
            raise ValueError('Invalid launch profile')
        args = fields.get('arguments')
        paths = fields.get('previous_paths', [])
        values = (fields.get('path', ''), fields.get('desktop', ''))
        if any(not isinstance(value, str) or '\0' in value for value in values):
            raise ValueError('Invalid launch paths')
        for items in (args if args is not None else [], paths):
            if not isinstance(items, list) or any(not isinstance(item, str) or '\0' in item for item in items):
                raise ValueError('Invalid launch arguments or previous paths')
        return LaunchProfile(fields.get('mode', 'auto'), values[0], tuple(args) if args is not None else None,
                             values[1], tuple(paths))

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def save(self, key, profile):
        with _LOCK:
            data = self.read()
            data[key] = asdict(profile)
            self._write(data)

    @contextmanager
    def relocate_bin(self, scope, paths, destination):
        moves = {path: str(destination / Path(path).name) for path in paths
                 if os.path.isabs(path) and path != str(destination / Path(path).name)}
        if not moves:
            yield
            return
        if len(set(moves.values())) != len(moves):
            raise ValueError('Conflicting registered names in launch profiles')
        with _LOCK:
            original = self.read()
            updated = dict(original)
            for old, new in moves.items():
                old_key = f'bin:{scope}:{old}'
                new_key = f'bin:{scope}:{new}'
                profile = self.profile(original, old_key)
                updated.pop(old_key, None)
                path = new if (profile.path and path_identity(profile.path) == path_identity(old)) else profile.path
                aliases = tuple(dict.fromkeys((*profile.previous_paths, old)))
                updated[new_key] = asdict(replace(profile, path=path, previous_paths=aliases))
            self._write(updated)
            try:
                yield
            except BaseException:
                self._write(original)
                raise


def terminal_command():
    found = shutil.which('xdg-terminal-exec')
    if found:
        return (found,)
    desktop = set(os.environ.get('XDG_CURRENT_DESKTOP', '').lower().split(':'))
    choices = [
        ('kde', 'konsole', ('-e',)), ('xfce', 'xfce4-terminal', ('--disable-server', '-x')),
        ('mate', 'mate-terminal', ('-x',)), ('gnome', 'gnome-terminal', ('--',)),
        ('lxqt', 'qterminal', ('-e', '/usr/bin/env', '--')), ('', 'x-terminal-emulator', ('-e',)),
        ('', 'xterm', ('-e',)),
    ]
    for _, name, args in sorted(choices, key=lambda item: item[0] not in desktop):
        found = shutil.which(name)
        if found:
            return (found, *args)
    return ()


def resolve_launch(profile, choices, default_path=None, terminal_available=None):
    if profile.mode == 'disabled':
        return LaunchState(reason='launch.disabled')
    desktop = None
    if profile.desktop:
        desktop = next((command for _, command in choices if command.desktop == profile.desktop), None)
        if not desktop and (profile.mode == 'auto' or not profile.path):
            return LaunchState(reason='launch.desktop_unavailable')
    elif len(choices) == 1:
        desktop = choices[0][1]
    if profile.mode == 'auto' and not desktop:
        return LaunchState(reason='launch.multiple' if choices else 'launch.unknown')
    if not desktop and not profile.path and not default_path:
        return LaunchState(reason='launch.multiple' if choices else 'launch.executable_required')

    if profile.path or not desktop:
        path = executable_path(profile.path or default_path)
        if not path:
            return LaunchState(reason='launch.executable_unavailable')
        command = LaunchCommand((path, *(profile.arguments or ())), False, path)
    elif profile.arguments is not None:
        # Keep an env prefix from the desktop file and replace app arguments.
        index = desktop.argv.index(desktop.executable)
        command = replace(desktop, argv=(*desktop.argv[:index + 1], *profile.arguments))
    else:
        command = desktop
    terminal = desktop.terminal if profile.mode == 'auto' else profile.mode == 'terminal'
    command = replace(command, terminal=terminal)
    if terminal and not (terminal_command if terminal_available is None else terminal_available)():
        return LaunchState(command, 'launch.no_terminal')
    return LaunchState(command, None)


class LaunchablePackage:
    launch_state = LaunchState()
    launch_choices = ()
    launch_profile = LaunchProfile()
    launch_files = ()
    launch_executables = ()
    launch_managed_path = None

    def can_be_run(self):
        return self.installed and bool(self.launch_state.command) and self.launch_state.reason is None

    def get_launch_disabled_reason(self):
        return self.launch_state.reason

    def configure_launch_action(self):
        return CustomSoftwareAction('launch.configure', 'launch.configure', self.get_type_icon_path(),
                                    'configure_launch', requires_root=False, requires_confirmation=False)


class ApplicationLauncher:
    def __init__(self, i18n):
        self.i18n = i18n
        self.profiles = LaunchProfiles()

    @staticmethod
    def key(pkg):
        if pkg.get_type() == 'bin':
            return f'bin:{pkg.scope}:{pkg.path}'
        return f'{pkg.get_type()}:{pkg.id}'

    def populate(self, pkg, desktop_catalogue, profiles, managed_path=None, files=()):
        pkg.launch_managed_path = managed_path
        pkg.launch_files = tuple(files)
        paths = (managed_path,) if managed_path else pkg.launch_files
        pkg.launch_executables = tuple(path for path in paths if not path.endswith('.desktop')
                                       and os.path.isabs(path) and executable_path(path))
        if profiles is None:
            pkg.launch_state = LaunchState(reason='launch.settings_error')
            return
        try:
            pkg.launch_profile = self.profiles.profile(profiles, self.key(pkg))
        except ValueError:
            pkg.launch_state = LaunchState(reason='launch.settings_error')
            return
        pkg.launch_choices = desktop_catalogue.matching(pkg.launch_files, pkg.launch_executables,
                                                        managed_path, pkg.launch_profile.previous_paths)
        self._resolve(pkg)

    def saved_profiles(self):
        try:
            return self.profiles.read()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _default_path(pkg):
        if pkg.launch_managed_path:
            return pkg.launch_managed_path
        if len(pkg.launch_executables) == 1:
            return pkg.launch_executables[0]

    def _resolve(self, pkg):
        pkg.launch_state = resolve_launch(pkg.launch_profile, pkg.launch_choices, self._default_path(pkg))

    def configure(self, pkg, watcher):
        current = pkg.launch_profile
        mode_options = [InputOption(self.i18n[f'launch.mode.{mode}'], mode) for mode in _MODES]
        mode_field = SingleSelectComponent(SelectViewType.COMBO, self.i18n['launch.mode'], mode_options,
                                           next(option for option in mode_options if option.value == current.mode))
        path_field = TextInputComponent(self.i18n['launch.executable'], value=current.path or pkg.launch_managed_path or '',
                                        placeholder=self._default_path(pkg), min_width=300,
                                        suggestions=list(pkg.launch_executables) if len(pkg.launch_executables) > 1 else None)
        args_field = TextInputComponent(self.i18n['launch.arguments'], value=shlex.join(current.arguments or ()),
                                        tooltip=self.i18n['launch.arguments.tip'])
        fields = [mode_field, path_field, args_field]
        desktop_field = None
        if pkg.launch_choices or current.desktop:
            choices = [InputOption(self.i18n['launch.desktop.auto'], '')]
            choices.extend(InputOption(f'{name} — {Path(command.desktop).name}', command.desktop, tooltip=command.desktop)
                           for name, command in pkg.launch_choices)
            if current.desktop and not any(option.value == current.desktop for option in choices):
                choices.append(InputOption(self.i18n['launch.desktop_unavailable'], current.desktop, tooltip=current.desktop))
            desktop_field = SingleSelectComponent(SelectViewType.COMBO, self.i18n['launch.desktop'], choices,
                                                  next(option for option in choices if option.value == current.desktop),
                                                  max_width=520)
            fields.append(desktop_field)
        while True:
            confirmed = watcher.request_confirmation(title=self.i18n['launch.configure'], body=self.i18n['launch.help'],
                                                      components=[FormComponent(fields, spaces=False)],
                                                      confirmation_label=self.i18n['launch.save'],
                                                      deny_label=self.i18n['cancel'].capitalize(), window_cancel=True,
                                                      scroll_content=False)
            # Reopening a form renders new widgets for the same field values.
            path_field.observers.clear()
            args_field.observers.clear()
            if not confirmed:
                return False
            try:
                path = path_field.get_value().strip()
                mode = mode_field.get_selected()
                if path and path == pkg.launch_managed_path and not current.path:
                    path = ''
                if path and mode != 'disabled':
                    path = os.path.abspath(os.path.expanduser(path)) if path.startswith('~/') else path
                    if not os.path.isabs(path) or not executable_path(path):
                        raise ValueError(self.i18n['launch.executable_unavailable'])
                    path = os.path.normpath(path)
                text = args_field.get_value()
                try:
                    arguments = tuple(shlex.split(text)) if text.strip() else None
                except ValueError:
                    raise ValueError(self.i18n['launch.arguments_invalid']) from None
                if arguments and any('\0' in value for value in arguments):
                    raise ValueError(self.i18n['launch.arguments_invalid'])
                profile = replace(current, mode=mode, path=path, arguments=arguments,
                                  desktop=desktop_field.get_selected() if desktop_field else '')
                state = resolve_launch(profile, pkg.launch_choices, self._default_path(pkg))
                if profile.mode in {'gui', 'terminal'} and not state.command:
                    raise ValueError(self.i18n[state.reason])
                self.profiles.save(self.key(pkg), profile)
                pkg.launch_profile = profile
                pkg.launch_state = state
                return True
            except (OSError, ValueError) as error:
                watcher.show_message(self.i18n['launch.configure'], escape(str(error)), MessageType.ERROR)

    def launch(self, pkg):
        state = pkg.launch_state
        if not pkg.can_be_run() or not state.command:
            raise RuntimeError(self.i18n[state.reason or 'launch.unknown'])
        command = state.command
        if not executable_path(command.executable) or not executable_path(command.argv[0]):
            raise RuntimeError(self.i18n['launch.executable_unavailable'])
        if os.geteuid() == 0:
            raise RuntimeError(self.i18n['launch.root'])
        terminal = terminal_command() if command.terminal else ()
        if command.terminal and not terminal:
            raise RuntimeError(self.i18n['launch.no_terminal'])
        child = subprocess.Popen([*terminal, *command.argv], cwd=command.cwd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        Thread(target=child.wait, daemon=True).start()

    def information(self, pkg):
        command = pkg.launch_state.command
        paths = pkg.launch_executables
        unknown = self.i18n['launch.info.unknown']
        info = {
            'binary_path': command.executable if command else paths[0] if len(paths) == 1 else unknown,
            'launch_command': shlex.join(command.argv) if command else unknown,
        }
        if len(paths) > 1:
            info['executables'] = InstalledFiles(self.i18n['launch.info.path_count'].format(len(paths)), list(paths))
        desktops = list(dict.fromkeys(
            [choice.desktop for _, choice in pkg.launch_choices] +
            [path for path in pkg.launch_files if path.endswith('.desktop')]
        ))
        if command and command.desktop:
            info['desktop_file'] = command.desktop
        elif len(desktops) == 1:
            info['desktop_file'] = desktops[0]
        elif desktops:
            info['desktop_file'] = InstalledFiles(self.i18n['launch.info.path_count'].format(len(desktops)), desktops)
        if pkg.launch_files:
            # Many DEBs have files under both /usr and /opt. A generic root
            # directory is not a meaningful package location in that case.
            base = os.path.commonpath(pkg.launch_files)
            if base in pkg.launch_files:
                base = os.path.dirname(base)
            location = base if base and base != '/' else self.i18n['launch.info.file_locations']
            info['app_files'] = InstalledFiles(location, list(pkg.launch_files))
        return info
