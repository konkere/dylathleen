import json
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html import escape
from pathlib import Path
from tarfile import TarError
from threading import RLock
from zipfile import BadZipFile

import requests

from dylathleen.api.abstract.controller import SearchResult, SettingsController, SettingsView, SoftwareManager, TransactionResult
from dylathleen.api.abstract.model import CustomSoftwareAction, PackageUpdate
from dylathleen.api.abstract.view import FormComponent, InputOption, MessageType, PanelComponent, SelectViewType, SingleSelectComponent, TextInputComponent
from dylathleen.api.paths import CONFIG_DIR
from dylathleen.api.user import is_root
from dylathleen.gems.bin.client import BinCommand, BinRegistry, ChoiceRequired, SystemAccessRequired
from dylathleen.gems.bin.model import ICON, BinApplication
from dylathleen.gems.bin.registry import BinError, absolute_path, write_json
from dylathleen.gems.bin.releases import Repository, same_variant

FAILURES = (BinError, OSError, ValueError, KeyError, EOFError, requests.RequestException, subprocess.SubprocessError,
            TarError, BadZipFile)
DEFAULT_DIRECTORIES = {'user': '~/.local/bin', 'system': '/usr/local/bin'}


class Cancelled(BinError):
    pass


class BinManager(SoftwareManager, SettingsController):

    def __init__(self, context):
        super().__init__(context)
        self.i18n = context.i18n
        self.logger = context.logger
        self.enabled = True
        self.registries = {system: BinRegistry(system) for system in (False, True)}
        self.settings_path = Path(CONFIG_DIR) / 'bin.json'
        self.lock = RLock()
        self.system_entries = {}
        self.registry_errors = {}
        self.update_errors = {}

    def can_work(self):
        return (True, None) if self._executable() else (False, self.i18n['bin.unavailable'])

    def _executable(self):
        executable = shutil.which('bin')
        if executable:
            return executable
        try:
            for value in self._directories().values():
                executable = shutil.which(str(self._directory(value) / 'bin'))
                if executable:
                    return executable
        except FAILURES:
            return None

    def _command(self, entry=None):
        executable = self._executable()
        if not executable:
            raise BinError(self.i18n['bin.unavailable'])
        return BinCommand(executable, entry)

    def is_enabled(self):
        return self.enabled

    def set_enabled(self, enabled):
        self.enabled = enabled

    def is_default_enabled(self):
        return True

    def get_managed_types(self):
        return {BinApplication}

    def requires_root(self, action, pkg):
        # Request authorization at the write step, after network calls and GUI choices.
        return False

    def prepare(self, task_manager, root_password, internet_available):
        pass

    def _warn(self, text):
        self.logger.warning('bin: %s', text)

    def _read(self, system):
        try:
            entries = self.registries[system].read()['bins']
        except SystemAccessRequired:
            self.registry_errors.pop(system, None)
            return self.system_entries
        except FAILURES as error:
            self.registry_errors[system] = str(error)
            self._warn(error)
            return self.system_entries if system else {}
        self.registry_errors.pop(system, None)
        if system:
            self.system_entries = entries
        return entries

    def _applications(self):
        with self.lock:
            scopes = (False, True) if not is_root() else (True,)
            return [BinApplication(key, entry, system, self.i18n)
                    for system in scopes for key, entry in self._read(system).items()]

    def _check_update(self, pkg):
        if pkg.pinned:
            return
        try:
            with self._command(pkg.entry) as command:
                update = command.check()
            if update:
                pkg.update = True
                pkg.latest_version = update.version
        except FAILURES as error:
            pkg.update_error = str(error)
            pkg.description += f" · {self.i18n['bin.check_failed']}"
            self._warn(f'{pkg.name} ({pkg.scope}): {error}')

    def _scan(self, apps):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self._check_update, apps))
        with self.lock:
            self.update_errors = {pkg.id: (pkg.name, pkg.description, pkg.update_error)
                                  for pkg in apps if pkg.update_error}
        return apps

    def read_installed(self, disk_loader, limit, only_apps, pkg_types, internet_available):
        if pkg_types is not None and BinApplication not in pkg_types:
            return SearchResult.empty()
        apps = self._applications()
        if internet_available:
            self._scan(apps)
        if limit > 0:
            apps = apps[:limit]
        return SearchResult(installed=apps, new=[], total=len(apps))

    def search(self, words, disk_loader, limit):
        term = words.strip().casefold()
        apps = [app for app in self._applications()
                if term in app.name.casefold() or term in app.source.casefold()]
        if limit > 0:
            apps = apps[:limit]
        return SearchResult(installed=apps, new=[], total=len(apps))

    def list_updates(self, internet_available):
        if not internet_available:
            return []
        return [PackageUpdate(pkg.id, pkg.latest_version, pkg.get_type(), pkg.name)
                for pkg in self._scan(self._applications()) if pkg.update]

    def list_warnings(self, internet_available):
        with self.lock:
            return [escape(message) for message in self.registry_errors.values()] + [
                escape(f'{name} ({scope}): {message}') for name, scope, message in self.update_errors.values()
            ]

    def get_info(self, pkg):
        info = {'name': pkg.name, 'version': pkg.version, 'scope': pkg.description,
                'source': pkg.source, 'provider': pkg.entry.get('provider'),
                'binary_path': pkg.path, 'asset': pkg.entry.get('selected_asset'),
                'member': pkg.entry.get('package_path'), 'update_error': pkg.update_error}
        if pkg.executable():
            info['launch_command'] = shlex.quote(pkg.path)
        return {key: value for key, value in info.items() if value}

    def get_history(self, pkg):
        pass

    def launch(self, pkg):
        pass

    def downgrade(self, pkg, root_password, handler):
        return False

    def install(self, pkg, root_password, disk_loader, watcher):
        # URL-based installation is available through the global action menu.
        return TransactionResult.fail()

    def gen_custom_actions(self):
        for name, online in (('install_url', True), ('read_system', False)):
            key = f'bin.action.{name}'
            yield CustomSoftwareAction(key, key, ICON, name, requires_root=False,
                                       requires_confirmation=False, requires_internet=online)

    def _system_call(self, method, *args, watcher=None, password=None):
        try:
            return method(*args, password=password)
        except SystemAccessRequired as error:
            if password is not None or watcher is None or not shutil.which('sudo'):
                raise BinError(self.i18n['bin.system.locked'] + f' {error}') from error
            valid, password = watcher.request_root_password()
            if not valid:
                raise Cancelled() from error
            return method(*args, password=password)

    def _error(self, watcher, error):
        self._warn(error)
        watcher.print(str(error))
        watcher.show_message(self.i18n['error'].capitalize(), escape(str(error)), MessageType.ERROR)

    def read_system(self, root_password, watcher):
        try:
            entries = self._system_call(self.registries[True].read, watcher=watcher, password=root_password)
            with self.lock:
                self.system_entries = entries['bins']
                self.registry_errors.pop(True, None)
            return True
        except Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def _ask(self, watcher, components, body=None):
        if not watcher.request_confirmation(title=self.i18n['bin.title'], body=body,
                                            components=[FormComponent(components)],
                                            confirmation_label=self.i18n['proceed'].capitalize(),
                                            deny_label=self.i18n['cancel'].capitalize(), window_cancel=True):
            raise Cancelled()

    def _select(self, key, pairs, default=None):
        options = [InputOption(label=label, value=value) for label, value in pairs]
        selected = next((option for option in options if option.value == default), None)
        if selected is None:
            selected = InputOption(label=self.i18n['bin.select'], value=None)
            options.insert(0, selected)
        return SingleSelectComponent(SelectViewType.COMBO, self.i18n[key], options, selected, max_width=560,
                                     capitalize_label=False)

    def _choose(self, watcher, key, values, label, body=None):
        if not values:
            raise BinError(self.i18n['bin.no_options'])
        if len(values) == 1:
            return values[0]
        field = self._select(key, [(label(value), value) for value in values])
        while field.get_selected() is None:
            self._ask(watcher, [field], body)
        return field.get_selected()

    def _install_release(self, command, repo, release, watcher, body=None):
        asset = self._choose(watcher, 'bin.info.asset', release.assets, lambda a: a.name, body)
        watcher.change_substatus(self.i18n['bin.fetching'])
        try:
            return command.install(release.url, asset.name)
        except ChoiceRequired:
            members = repo.archive_members(asset)
        member = self._choose(watcher, 'bin.info.member', members, str, escape(asset.name))
        return command.install(release.url, asset.name, member)

    def _directories(self):
        try:
            return {**DEFAULT_DIRECTORIES, **json.loads(self.settings_path.read_text())}
        except FileNotFoundError:
            return DEFAULT_DIRECTORIES.copy()

    def _directory(self, value):
        value = value.strip()
        if value == '~' or value.startswith('~/'):
            value = str(Path.home() / value[1:].lstrip('/'))
        return absolute_path(value)

    def _save(self, scope, key, expected, entry, watcher, artifact=None, password=None):
        def save(password):
            return self.registries[scope == 'system'].save(
                key, expected, entry, self._directory(self._directories()[scope]), artifact, password)

        with self.lock:
            if scope == 'system':
                self.system_entries = self._system_call(save, watcher=watcher, password=password)['bins']
            else:
                save(None)
            self.update_errors.pop(f'{scope}:{key}', None)

    def install_url(self, root_password, watcher):
        try:
            source = TextInputComponent(self.i18n['bin.info.source'], min_width=440)
            self._ask(watcher, [source])
            watcher.change_substatus(self.i18n['bin.fetching'])
            repo = Repository(source.get_value())
            releases, latest = repo.releases()
            if not releases:
                raise BinError(self.i18n['bin.no_releases'])
            directories = self._directories()
            scopes = ('user', 'system') if not is_root() else ('system',)
            scope = self._select('bin.info.scope', [
                (f"{self.i18n[f'bin.scope.{name}']} ({directories[name]})", name) for name in scopes
            ], scopes[0])
            tag = self._select('bin.release', [
                (f"{release.version} ({self.i18n['bin.prerelease']})" if release.prerelease else release.version, release)
                for release in releases
            ], latest)
            name = TextInputComponent(self.i18n['bin.name'], value=repo.name)
            self._ask(watcher, [tag, scope, name])
            name = name.get_value().strip()
            if not name or Path(name).name != name or name in ('.', '..'):
                raise BinError(self.i18n['bin.invalid_name'])
            directory = self._directory(directories[scope.get_selected()])
            key = str(absolute_path(str(directory / name)))
            with self._command() as command:
                entry = self._install_release(command, repo, tag.get_selected(), watcher)
                if entry.get('version') != tag.get_selected().version:
                    raise BinError(self.i18n['bin.release_changed'])
                self._save(scope.get_selected(), key, None, entry, watcher, command.target, root_password)
            return True
        except Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def _state(self, pkg, verb, root_password, watcher):
        try:
            with self._command(pkg.entry) as command:
                entry = command.set_state(verb)
            # State commands modify only the native registry entry.
            if entry:
                entry = {**pkg.entry, 'pinned': entry['pinned']}
            self._save(pkg.scope, pkg.key, pkg.entry, entry, watcher, password=root_password)
            return True
        except Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def pin(self, pkg, root_password, watcher):
        return self._state(pkg, 'pin', root_password, watcher)

    def unpin(self, pkg, root_password, watcher):
        return self._state(pkg, 'unpin', root_password, watcher)

    def uninstall(self, pkg, root_password, watcher, disk_loader):
        if self._state(pkg, 'remove', root_password, watcher):
            return TransactionResult(True, [], [pkg])
        return TransactionResult.fail()

    def _update(self, pkg, watcher, password):
        watcher.change_substatus(f"{self.i18n['bin.fetching']} {pkg.name}")
        with self._command(pkg.entry) as command:
            update = command.check()
            if not update:
                return
            try:
                entry = command.update()
                if entry.get('version') != update.version:
                    raise BinError(self.i18n['bin.release_changed'])
                for field in ('selected_asset', 'package_path'):
                    previous = pkg.entry.get(field)
                    current = entry.get(field)
                    if previous and not same_variant(previous, pkg.version, current or '', entry['version']):
                        raise ChoiceRequired()
            except ChoiceRequired as error:
                try:
                    repo = Repository(pkg.source, existing=True)
                except BinError:
                    raise error
                # Upstream update has no --name. Install the exact replacement
                # into a fresh stage, then commit it to the same registered path.
                release = repo.release(update.version)
                if update.url and repo.host != 'gitlab.com':
                    release = replace(release, url=update.url)
                with self._command() as selected:
                    entry = self._install_release(selected, repo, release, watcher,
                                                  self.i18n['bin.update.select'].format(escape(pkg.name), escape(update.version)))
                    if entry.get('version') != update.version:
                        raise BinError(self.i18n['bin.release_changed'])
                    self._save(pkg.scope, pkg.key, pkg.entry, entry, watcher, selected.target, password)
                return
            self._save(pkg.scope, pkg.key, pkg.entry, entry, watcher, command.target, password)

    def upgrade(self, requirements, root_password, watcher):
        success = True
        items = requirements.to_upgrade
        for index, requirement in enumerate(items):
            if watcher.should_stop():
                return False
            try:
                self._update(requirement.pkg, watcher, root_password)
            except Cancelled:
                return False
            except FAILURES as error:
                self._error(watcher, error)
                success = False
            watcher.change_progress(round(100 * (index + 1) / len(items)))
        return success

    def get_settings(self):
        fields = [TextInputComponent(self.i18n[f'bin.directory.{scope}'], value=value, id_=scope, min_width=320)
                  for scope, value in self._directories().items() if scope in DEFAULT_DIRECTORIES]
        yield SettingsView(self, PanelComponent([FormComponent(fields)]), label='bin', icon_path=ICON,
                           save_callback=self.save_settings)

    def save_settings(self, component, watcher=None):
        with self.lock:
            try:
                previous = self._directories()
                form = component.get_component_by_idx(0, FormComponent)
                values = {scope: form.get_component(scope, TextInputComponent).get_value().strip()
                          for scope in DEFAULT_DIRECTORIES}
                directories = {scope: self._directory(value) for scope, value in values.items()}
                for scope in DEFAULT_DIRECTORIES:
                    if directories[scope] == self._directory(previous[scope]):
                        continue
                    system = scope == 'system'
                    current = {**previous, scope: values[scope]}
                    self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                    write_json(self.settings_path, current)
                    try:
                        if system:
                            result = self._system_call(self.registries[True].migrate, directories[scope], watcher=watcher)
                            self.system_entries = result['bins']
                        else:
                            self.registries[False].migrate(directories[scope])
                    except Exception:
                        write_json(self.settings_path, previous)
                        raise
                    previous = current
                return True, None
            except Cancelled:
                return False, [self.i18n['bin.settings.cancelled']]
            except FAILURES as error:
                self._warn(error)
                return False, [escape(str(error))]
