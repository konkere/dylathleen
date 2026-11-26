import difflib
import json
import os
import re
import shutil
import subprocess
import time
from html import escape
from pathlib import Path
from threading import RLock

import requests

from dylathleen.api.abstract.controller import SearchResult, SettingsController, SettingsView, SoftwareManager, TransactionResult
from dylathleen.api.abstract.model import PackageUpdate
from dylathleen.api.abstract.view import (FileChooserComponent, FormComponent, InputOption, MessageType, MultipleSelectComponent,
                                          PanelComponent, SelectViewType, SingleSelectComponent, TextInputComponent, TextInputType)
from dylathleen.daemon import client as backend
from dylathleen.api.paths import CONFIG_DIR, CACHE_DIR
from dylathleen.commons.app_launch import ApplicationLauncher
from dylathleen.commons.desktop_entry import DesktopCatalogue
from dylathleen.gems.debget import catalogue, client, generator
from dylathleen.gems.debget.editor_model import RecipeEditorComponent
from dylathleen.gems.debget.model import ICON, DebGetApplication, action
from dylathleen.gems.debget.privileged import DebGetError, ROOT, NATIVE, atomic_file, hash_text, package, recipe_text

FAILURES = (DebGetError, OSError, ValueError, KeyError, subprocess.SubprocessError, requests.RequestException)
CHECK_FAILURE = re.compile(r'rate limit|failed|empty or missing|missing required|unable to|could not|ERROR!|skipping', re.I)
SETTINGS = {'include_unsupported': False, 'refresh_interval': 5}


class DebGetManager(SoftwareManager, SettingsController):
    def __init__(self, context):
        super().__init__(context)
        self.i18n = context.i18n
        self.enabled = True
        self.logger = context.logger
        self.version = None
        self.version_at = 0
        self.updates = {}
        self.system_snapshot = {}
        self.stale = set()
        self.inventory_warning = None
        self.resolved = {}
        self.last_check = 0
        self.official_notified = set()
        self.warning = None
        self.lock = RLock()
        self.settings_path = Path(CONFIG_DIR) / 'debget.json'
        self.log_path = Path(CACHE_DIR) / 'debget-last.log'
        self.launcher = ApplicationLauncher(self.i18n)
        self.actions = {name: action(name, online, refresh) for name, online, refresh in (
            ('refresh_catalogue', True, True), ('check_updates', True, True), ('bulk_upgrade', True, True),
            ('browse_catalogue', False, True), ('manage_recipes', False, True),
            ('generate', True, True), ('show_log', False, False),
            ('resolve', True, False), ('reinstall', True, True), ('purge', False, True),
            ('upgrade_one', True, True),
        )}

    def can_work(self):
        if not os.access(NATIVE, os.X_OK) or os.getuid() == 0:
            return False, self.i18n['debget.unavailable']
        return True, None

    def is_enabled(self):
        return self.enabled

    def set_enabled(self, enabled):
        self.enabled = enabled

    def is_default_enabled(self):
        return True

    def get_managed_types(self):
        return {DebGetApplication}

    def requires_root(self, action, pkg):
        return False  # Request credentials only after the specific operation is confirmed.

    def prepare(self, task_manager, root_password, internet_available):
        self._detect()

    def _detect(self):
        if time.monotonic() - self.version_at > 60 or self.version is None:
            try:
                self.version = client.native_version()
                if self.version != '0.4.7':
                    self.warning = self.i18n['debget.unsupported_version'].format(self.version)
            except FAILURES as error:
                self.logger.warning('deb-get: %s', error)
            finally:
                self.version_at = time.monotonic()

    def _config(self):
        try:
            return {**SETTINGS, **json.loads(self.settings_path.read_text())}
        except (OSError, ValueError, TypeError):
            return SETTINGS.copy()

    def _apps(self, read_launch=True):
        if 'debget' in self.system_snapshot:
            recipes = catalogue.read_catalogue()
            state = self.system_snapshot['debget']
            registry = {name: row[2] for name, row in state.items()}
            statuses = {name: row[:2] for name, row in state.items()}
        else:
            recipes, registry, statuses = catalogue.inventory()
        arch, codename, distro = catalogue.machine()
        settings = self._config()
        with self.lock:
            updates = self.updates.copy()
        result = []
        for name in sorted(set(recipes) | set(statuses)):
            choices = recipes.get(name, [])
            recipe = next((r for r in choices if not r.deprecated), choices[0] if choices else None)
            status = statuses.get(name)
            compatible = bool(recipe and not recipe.deprecated and recipe.compatible(arch, codename, distro))
            if status or recipe and (settings['include_unsupported'] or compatible):
                available = updates.get(name)
                if status and available == status[0]:
                    available = None
                result.append(DebGetApplication(name, recipe, status, registry.get(name), available, self.actions, compatible))
        for app in result:
            if app.installed and (self.inventory_warning or 'debget:' + app.deb_name in self.stale):
                app.description = (app.description or '') + ' · ' + self.i18n['debget.stale']
        installed = [app for app in result if app.installed] if read_launch else []
        if installed:
            desktops = DesktopCatalogue()
            profiles = self.launcher.saved_profiles()
            for app in installed:
                try:
                    files = catalogue.installed_files(app.deb_name)
                except (OSError, subprocess.SubprocessError, UnicodeError) as error:
                    self.logger.warning('deb-get: could not read files for %s: %s', app.deb_name, error)
                    files = ()
                self.launcher.populate(app, desktops, profiles, files=files)
        return result

    def _read_apps(self, read_launch=True):
        try:
            return self._apps(read_launch)
        except FAILURES as error:
            self.warning = str(error)
            self.logger.warning('deb-get: %s', error)
            return []

    def _refresh(self, online):
        data, warning = backend.refresh(online)
        with self.lock:
            self.inventory_warning = warning or data.get('errors', {}).get('debget')
            self.stale = set(data.get('stale', ()))
            self.system_snapshot = data
            if 'updates' in data:
                self.updates = {k[7:]: v for k, v in data['updates'].items() if k.startswith('debget:')}

    def read_installed(self, disk_loader, limit, only_apps, pkg_types, internet_available):
        if pkg_types and DebGetApplication not in pkg_types:
            return SearchResult.empty()
        self._refresh(internet_available)
        apps = [p for p in self._read_apps() if p.installed]
        if limit > 0:
            apps = apps[:limit]
        return SearchResult(installed=apps, new=[], total=len(apps))

    def search(self, words, disk_loader, limit):
        term = words.casefold()
        apps = [p for p in self._read_apps()
                if term in {'deb-get', 'debget'} or term in f'{p.name} {p.deb_name} {p.description}'.casefold()]
        if limit > 0:
            apps = apps[:limit]
        return SearchResult([p for p in apps if p.installed], [p for p in apps if not p.installed], len(apps))

    def list_updates(self, internet_available):
        if not internet_available:
            return []
        self._refresh(True)
        return [PackageUpdate(p.id, p.latest_version, p.get_type(), p.name)
                for p in self._read_apps(read_launch=False) if p.update]

    def list_warnings(self, internet_available):
        return [escape(msg) for msg in (self.warning, self.inventory_warning) if msg]

    def get_info(self, pkg):
        source = pkg.recipe.fields if pkg.recipe else {}
        details = self.resolved.get(pkg.deb_name, {})
        catalogues = catalogue.read_catalogue().get(pkg.deb_name, [])
        if pkg.method in {'apt', 'ppa'}:
            try:
                cached = catalogue.apt_candidate(pkg.deb_name)
            except FAILURES:
                cached = None
        else:
            cached = None
        info = {'name': pkg.name, 'package': pkg.deb_name, 'version': pkg.version, 'description': pkg.description,
                'repository': pkg.recipe.repo if pkg.recipe else None, 'method': pkg.method,
                'manager_version': self.version, 'website': source.get('WEBSITE'),
                'architectures': details.get('Architecture') or source.get('ARCHS_SUPPORTED'),
                'codenames': source.get('CODENAMES_SUPPORTED'),
                'source': source.get('APT_REPO_URL') or source.get('PPA') or details.get('Download') or source.get('URL')
                or (details.get('Launchpad') or details.get('Repository') if pkg.method in {'apt', 'ppa'} else None),
                'published': details.get('Published') or pkg.latest_version if pkg.update else details.get('Published'),
                'apt_cached_candidate': cached,
                'overrides': ', '.join(recipe.repo for recipe in catalogues) if len(catalogues) > 1 else None}
        if pkg.installed:
            info.update(self.launcher.information(pkg))
            info.setdefault('app_files', self.i18n['launch.info.unknown'])
        return {k: v for k, v in info.items() if v}

    def get_history(self, pkg):
        pass

    def launch(self, pkg):
        self.launcher.launch(pkg)

    def configure_launch(self, pkg, root_password, watcher):
        return self.launcher.configure(pkg, watcher)

    def downgrade(self, pkg, root_password, handler):
        return False

    def _title(self):
        return self.i18n['gem.debget.label']

    def _confirm(self, watcher, text=None, components=None, confirm_key='proceed'):
        body = (f'<p style="white-space: pre-wrap">{escape(text)}</p>' if text else None)
        has_text_view = any(isinstance(c, RecipeEditorComponent)
                            or isinstance(c, TextInputComponent) and c.type == TextInputType.MULTIPLE_LINES
                            for c in components or ())
        return watcher.request_confirmation(title=self._title(), body=body, components=components,
                                            confirmation_label=self.i18n[confirm_key].capitalize(),
                                            deny_label=self.i18n['cancel'].capitalize(), window_cancel=True,
                                            scroll_content=not has_text_view)

    def _ask(self, watcher, text=None, components=None, confirm_key='proceed'):
        if not self._confirm(watcher, text, components, confirm_key):
            raise client.Cancelled()

    def _error(self, watcher, error):
        message = str(error)
        self.logger.warning('deb-get: %s', message)
        watcher.print(message)
        watcher.show_message(self._title(), escape(message), MessageType.ERROR)

    def _native(self, command, watcher, password, pkg=None):
        names = [pkg.deb_name] if pkg else []
        with self.lock:
            try:
                response = client.invoke({'operation': 'native', 'action': command, 'packages': names}, watcher,
                                         lambda text: self._confirm(watcher, components=[self._readonly(text)]), password)
            except FAILURES as error:
                self._save_log([str(error)])
                raise
            self._save_log(response[0])
            watcher.change_progress(100)
            if pkg and command != 'show':
                self.updates.pop(pkg.deb_name, None)
                self.resolved.pop(pkg.deb_name, None)
            if command == 'upgrade':
                self.updates.clear()
                self.resolved.clear()
            if pkg and pkg.deb_name == 'deb-get' or command == 'upgrade':
                self.version_at = 0
                self._detect()
            return response[0]

    def _save_log(self, lines):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_file(self.log_path, '\n'.join(lines[-400:]), 0o600)
        except OSError as error:
            self.logger.warning('deb-get log: %s', error)

    def _readonly(self, value):
        return TextInputComponent(self.i18n['debget.output'], value=value, type_=TextInputType.MULTIPLE_LINES,
                                  read_only=True, min_width=420, min_height=160)

    def _transaction(self, pkg, verb, password, watcher):
        name = pkg.deb_name
        scope = self.i18n['debget.trust_one'].format(name)
        self._ask(watcher, self.i18n['debget.action.' + verb.replace('-', '_') if verb != 'remove' else 'debget.remove'] + '\n\n' + scope)
        watcher.change_substatus(name)
        self._native(verb, watcher, password, pkg)
        info = catalogue.installed_versions([name]).get(name)
        marked = name in catalogue.registered()
        if verb == 'remove' and (marked or info and info[1] == 'installed') or verb == 'purge' and (marked or info):
            raise DebGetError(self.i18n['debget.state_unchanged'])
        if verb in {'install', 'reinstall', 'upgrade_one', 'upgrade-one'} and (not info or info[1] != 'installed' or not marked):
            raise DebGetError(self.i18n['debget.state_unchanged'])

    def _apply(self, pkg, verb, password, watcher):
        try:
            self._transaction(pkg, verb, password, watcher)
            return True
        except client.Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def install(self, pkg, root_password, disk_loader, watcher):
        if self._apply(pkg, 'install', root_password, watcher):
            app = next((item for item in self._read_apps() if item.deb_name == pkg.deb_name), pkg)
            return TransactionResult(True, [app], [])
        return TransactionResult.fail()

    def uninstall(self, pkg, root_password, watcher, disk_loader):
        if self._apply(pkg, 'remove', root_password, watcher):
            return TransactionResult(True, [], [pkg])
        return TransactionResult.fail()

    def upgrade(self, requirements, root_password, watcher):
        success = True
        for index, item in enumerate(requirements.to_upgrade):
            if watcher.should_stop():
                return False
            success = self._apply(item.pkg, 'install', root_password, watcher) and success
            watcher.change_progress(round((index + 1) * 100 / len(requirements.to_upgrade)))
        return success

    def purge(self, pkg, root_password, watcher):
        return self._apply(pkg, 'purge', root_password, watcher)

    def reinstall(self, pkg, root_password, watcher):
        return self._apply(pkg, 'reinstall', root_password, watcher)

    def upgrade_one(self, pkg, root_password, watcher):
        return self._apply(pkg, 'upgrade-one', root_password, watcher)

    def resolve(self, pkg, root_password, watcher):
        try:
            self._ask(watcher, self.i18n['debget.trust_one'].format(pkg.deb_name))
            lines = self._native('show', watcher, root_password, pkg)
            resolved = client.parse_show(lines)
            if resolved.get('Package') != pkg.deb_name or pkg.method not in {'apt', 'ppa'} and (
                    not resolved.get('Download') or not resolved.get('Published')):
                raise DebGetError(self.i18n['debget.resolve_failed'])
            self.resolved[pkg.deb_name] = resolved
            self._confirm(watcher, components=[self._readonly('\n'.join(lines))])
            return True
        except client.Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def _catalogue_action(self, command, root_password, watcher):
        try:
            if command == 'update' and self.last_check and time.monotonic() - self.last_check < self._config()['refresh_interval'] * 60:
                watcher.show_message(self._title(), self.i18n['debget.recent'])
                return True
            recipes = catalogue.read_catalogue()
            local = [name for name, options in recipes.items() if any(r.repo == '99-local' for r in options)]
            sources = sorted({recipe.repo for options in recipes.values() for recipe in options})
            text = self.i18n['debget.trust_all'].format(', '.join(sorted(local)) or '—')
            text += '\n' + self.i18n['debget.catalogues'].format(', '.join(sources))
            if command == 'upgrade':
                text += '\n\n' + self.i18n['debget.bulk_notice']
            self._ask(watcher, text)
            lines = self._native(command, watcher, root_password)
            self.warning = None
            if command == 'update':
                with self.lock:
                    self.updates = client.pending_updates(lines)
                    self.last_check = time.monotonic()
            problems = [line for line in lines if CHECK_FAILURE.search(line)]
            if problems:
                self.warning = self.i18n['debget.partial'] + '\n' + '\n'.join(problems[-8:])
                watcher.show_message(self._title(), escape(self.warning), MessageType.WARNING)
            if command in {'catalogue', 'update'}:
                updated = catalogue.read_catalogue()
                overrides = {name for name in local if any(r.repo in {'01-main', '00-builtin'} and not r.deprecated
                                                           for r in updated.get(name, []))}
                collisions = sorted(overrides - self.official_notified)
                self.official_notified = overrides
                if collisions:
                    watcher.show_message(self._title(), escape(self.i18n['debget.official_found'].format(', '.join(collisions))),
                                         MessageType.WARNING)
            return True
        except client.Cancelled:
            return False
        except FAILURES as error:
            if command == 'update':
                self.warning = str(error)
            self._error(watcher, error)
            return False

    def refresh_catalogue(self, root_password, watcher):
        return self._catalogue_action('catalogue', root_password, watcher)

    def check_updates(self, root_password, watcher):
        return self._catalogue_action('update', root_password, watcher)

    def bulk_upgrade(self, root_password, watcher):
        return self._catalogue_action('upgrade', root_password, watcher)

    def show_log(self, root_password, watcher):
        try:
            text = self.log_path.read_text()
        except OSError:
            text = self.i18n['debget.no_log']
        self._confirm(watcher, components=[self._readonly(text)])
        return True

    def gen_custom_actions(self):
        for name in ('browse_catalogue', 'refresh_catalogue', 'check_updates', 'bulk_upgrade', 'manage_recipes', 'generate', 'show_log'):
            yield self.actions[name]

    def browse_catalogue(self, root_password, watcher):
        try:
            query = self._field('catalogue_search')
            catalogues = [('99-local', 'local'), ('01-main / 00-builtin', 'official'),
                          (self.i18n['debget.external'], 'external')]
            source = self._combo(self.i18n['debget.info.repository'], [(self.i18n['debget.all_catalogues'], '')] + catalogues)
            self._ask(watcher, self.i18n['debget.catalogue_help'], [FormComponent([query, source])])
            term = query.get_value().strip().casefold()
            category = source.get_selected()
            apps = [pkg for pkg in self._read_apps()
                    if term in f'{pkg.name} {pkg.deb_name} {pkg.description}'.casefold()
                    and (not category or 'debget-' + category in pkg.categories)]
            if not apps:
                raise DebGetError(self.i18n['debget.no_matches'])
            items = [(pkg.name if pkg.name == pkg.deb_name else f'{pkg.name} ({pkg.deb_name})', pkg)
                     for pkg in sorted(apps, key=lambda item: (item.name.casefold(), item.deb_name))]
            field = self._combo(self.i18n['debget.package'], items)
            self._ask(watcher, components=[field])
            pkg = field.get_selected()
            info = '\n'.join(f"{self.i18n.get('debget.info.' + key, key)}: {value}"
                             for key, value in self.get_info(pkg).items())
            if pkg.can_be_installed():
                if self._confirm(watcher, components=[self._readonly(info)], confirm_key='install'):
                    return self.install(pkg, root_password, None, watcher).success
            else:
                self._confirm(watcher, components=[self._readonly(info)])
            return True
        except client.Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def _combo(self, title, items, default=None):
        options = [InputOption(label, value) for label, value in items]
        selected = next((op for op in options if op.value == default), options[0])
        return SingleSelectComponent(SelectViewType.COMBO, title, options, selected, max_width=570)

    def _field(self, key, value=''):
        return TextInputComponent(self.i18n['debget.' + key], value=value, min_width=240, capitalize_label=False)

    def _editor(self, source, readonly=False):
        return RecipeEditorComponent(source, self.i18n['debget.warning.title'], self.i18n['debget.warning.body'], readonly)

    def _recipe_op(self, request, watcher, password):
        return client.invoke(request, watcher, lambda text: self._confirm(watcher, text), password)

    def _check_syntax(self, source, shellcheck=False):
        command = ['/bin/bash', '--noprofile', '--norc', '-n']
        if shellcheck:
            command = ['shellcheck', '-s', 'bash', '-']
        result = subprocess.run(command, input=source, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=15, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode and not shellcheck:
            raise DebGetError(result.stdout.strip())
        return result.stdout.strip() or self.i18n['debget.syntax_ok']

    def _edit(self, watcher, name, source, prior, root_password):
        editable_name = self._field('package', name) if prior is None else None
        editor = self._editor(source)
        menu = [('Save', 'save'), ('Bash -n', 'syntax')]
        menu[0] = (self.i18n['debget.save_recipe'], 'save')
        if shutil.which('shellcheck'):
            menu.append(('ShellCheck', 'shellcheck'))
        choice = self._combo(self.i18n['debget.recipe_tools'], menu, 'save')
        while True:
            components = ([FormComponent([editable_name], spaces=False)] if editable_name else []) + [editor, choice]
            self._ask(watcher, components=components)
            operation = choice.get_selected()
            if operation != 'save':
                try:
                    result = self._check_syntax(editor.value, shellcheck=operation == 'shellcheck')
                    self._confirm(watcher, components=[self._readonly(result)])
                except FAILURES as error:
                    self._error(watcher, error)
                continue
            name = package(editable_name.get_value().strip()) if editable_name else name
            self._check_syntax(editor.value)
            if prior is None and name in catalogue.read_catalogue():
                self._ask(watcher, self.i18n['debget.override'].format(name))
            self._ask(watcher, self.i18n['debget.write_recipe'].format(name))
            self._recipe_op({'operation': 'save', 'name': name, 'source': editor.value,
                             'expected': hash_text(prior) if prior is not None else None}, watcher, root_password)
            return True

    def manage_recipes(self, root_password, watcher):
        try:
            items = ('create', 'import', 'view', 'edit', 'resolve', 'export', 'delete', 'restore', 'compare', 'use_official')
            operation = self._combo(self.i18n['debget.recipe_tools'], [(self.i18n['debget.recipe.' + item], item)
                                                                      for item in items])
            self._ask(watcher, components=[operation])
            verb = operation.get_selected()
            recipes = catalogue.read_catalogue()
            local = {name: next(r for r in values if r.repo == '99-local') for name, values in recipes.items()
                     if any(r.repo == '99-local' for r in values)}
            if verb == 'create':
                return self._edit(watcher, '', 'DEFVER=1\nARCHS_SUPPORTED="amd64 arm64"\n', None, root_password)
            if verb == 'import':
                selection = FileChooserComponent(allowed_extensions=None, label=self.i18n['debget.recipe.import'])
                self._ask(watcher, components=[FormComponent([selection])])
                if not selection.file_path:
                    raise client.Cancelled()
                path = Path(selection.file_path)
                default = path.name if re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', path.name) else ''
                return self._edit(watcher, default, recipe_text(path), None, root_password)
            if verb == 'restore':
                text = self._field('package')
                self._ask(watcher, components=[FormComponent([text])])
                name = package(text.get_value().strip())
                _, events = self._recipe_op({'operation': 'backups', 'name': name}, watcher, root_password)
                backups = next((e['choices'] for e in events if e['event'] == 'backups'), [])
                if not backups:
                    raise DebGetError(self.i18n['debget.no_backups'])
                revision = self._combo(self.i18n['debget.recipe.restore'], [(h[:16], h) for h in backups])
                self._ask(watcher, self.i18n['debget.warning.body'], [revision])
                self._recipe_op({'operation': 'restore', 'name': name, 'revision': revision.get_selected(),
                                 'expected': hash_text(local[name].text) if name in local else None}, watcher, root_password)
                return True
            if not local:
                raise DebGetError(self.i18n['debget.no_local'])
            field = self._combo(self.i18n['debget.package'], [(name, name) for name in sorted(local)])
            self._ask(watcher, components=[field])
            name = field.get_selected()
            source = local[name].text
            if verb == 'view':
                self._confirm(watcher, components=[self._editor(source, True)])
                return True
            if verb == 'edit':
                return self._edit(watcher, name, source, source, root_password)
            if verb == 'resolve':
                selected = DebGetApplication(name, local[name], None, None, None, self.actions)
                return self.resolve(selected, root_password, watcher)
            if verb == 'export':
                destination = FileChooserComponent(allowed_extensions=None, label=self.i18n['debget.recipe.export'], directory=True)
                self._ask(watcher, components=[FormComponent([destination])])
                if not destination.file_path:
                    raise client.Cancelled()
                path = Path(destination.file_path) / name
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                        file.write(source)
                finally:
                    os.close(directory)
                watcher.print(str(path))
                return True
            if verb in {'compare', 'use_official'}:
                official = next((r for r in recipes[name] if r.repo in {'00-builtin', '01-main'} and not r.deprecated), None)
                if not official:
                    raise DebGetError(self.i18n['debget.no_official'])
                if next((r for r in recipes[name] if r.repo != '99-local' and not r.deprecated), None) != official and verb == 'use_official':
                    raise DebGetError(self.i18n['debget.other_override'])
                delta = '\n'.join(difflib.unified_diff(source.splitlines(), official.text.splitlines(),
                                                       '99-local/' + name, official.repo + '/' + name, lineterm=''))
                if verb == 'compare':
                    self._confirm(watcher, components=[self._editor(delta or self.i18n['debget.identical'], True)])
                    return True
                self._ask(watcher, self.i18n['debget.switch'], [self._editor(delta or source, True)])
            self._ask(watcher, self.i18n['debget.remove_recipe'].format(name))
            self._recipe_op({'operation': 'delete', 'name': name, 'expected': hash_text(source)}, watcher, root_password)
            return True
        except client.Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def generate(self, root_password, watcher):
        try:
            url = self._field('repository_url')
            self._ask(watcher, components=[FormComponent([url])])
            forge = generator.Forge(url.get_value(), client.user_token())
            watcher.change_substatus(self.i18n['debget.analyse'])
            summary, releases = forge.analyse()
            options = []
            by_slot = {}
            for asset in releases[0]:
                codename, arch = generator.guessed_platform(asset)
                filename = asset.name
                short_name = filename if len(filename) <= 48 else f'{filename[:22]}…{filename[-24:]}'
                option = InputOption(f'{short_name} · {codename or "any OS"} · {arch}', asset, tooltip=filename)
                options.append(option)
                by_slot.setdefault((codename, arch), []).append(option)
            selection = MultipleSelectComponent(self.i18n['debget.assets'], options,
                                                {items[0] for items in by_slot.values() if len(items) == 1})
            self._ask(watcher, self.i18n['debget.choose_assets'], [selection])
            assets = selection.get_selected_values()
            if not assets:
                raise DebGetError(self.i18n['debget.choose_assets'])
            name = None
            rules = []
            for asset in assets:
                watcher.change_substatus(asset.name)
                meta = generator.inspect_asset(asset)
                if name and name != meta.package:
                    raise DebGetError(self.i18n['debget.different_packages'])
                name = meta.package
                codename, arch = generator.guessed_platform(asset)
                if meta.arch != 'all':
                    arch = meta.arch
                platform = self._combo(self.i18n['debget.os'], [(self.i18n['debget.any_platform'], '')] +
                                       [(x, x) for x in ('debian', 'ubuntu', *catalogue.PLATFORM)], codename)
                processor = self._combo(self.i18n['debget.info.architectures'], [(x, x) for x in ('all', 'amd64', 'arm64')], arch)
                try:
                    inferred = generator.infer_rule(asset, meta, codename, arch, releases[1:])
                except DebGetError:
                    inferred = generator.Rule(codename, arch, asset.name, '', '')
                pattern = self._field('filename_rule', inferred.pattern)
                prefix = self._field('version_prefix', inferred.version_prefix)
                suffix = self._field('version_suffix', inferred.version_suffix)
                url_rule = self._field('url_rule', inferred.version_from_url)
                self._ask(watcher, f'{asset.name}\n{meta.package} {meta.version} ({meta.arch})\n' +
                          self.i18n['debget.rule_help'],
                          [FormComponent([platform, processor, pattern, prefix, suffix, url_rule], spaces=False)])
                rule = generator.Rule(platform.get_selected(), processor.get_selected(), pattern.get_value(),
                                      prefix.get_value(), suffix.get_value(), url_rule.get_value())
                generator.check_rule(rule, asset, meta, releases)
                rules.append(rule)
            pretty = self._field('pretty_name', forge.name)
            description = self._field('info.description', summary or name)
            self._ask(watcher, components=[FormComponent([pretty, description])])
            source = generator.recipe(forge, name, pretty.get_value().strip() or name,
                                      description.get_value().strip() or name, rules)
            self._check_syntax(source)
            return self._edit(watcher, name, source, None, root_password)
        except client.Cancelled:
            return False
        except FAILURES as error:
            self._error(watcher, error)
            return False

    def get_settings(self):
        settings = self._config()
        unsupported = self._combo(self.i18n['debget.unsupported'], [
            (self.i18n['yes'].capitalize(), True), (self.i18n['no'].capitalize(), False)], settings['include_unsupported'])
        interval = TextInputComponent(self.i18n['debget.refresh_interval'], value=str(settings['refresh_interval']), only_int=True)
        yield SettingsView(self, PanelComponent([FormComponent([unsupported, interval])]), 'deb-get', ICON)

    def save_settings(self, component):
        try:
            unsupported, interval = component.components[0].components
            minutes = interval.get_int_value()
            if minutes is None or not 0 <= minutes <= 120:
                raise DebGetError(self.i18n['debget.invalid_interval'])
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_file(self.settings_path, json.dumps({'include_unsupported': unsupported.get_selected(),
                                                        'refresh_interval': minutes}), 0o600)
            return True, None
        except FAILURES as error:
            return False, [escape(str(error))]
