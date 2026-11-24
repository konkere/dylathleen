import os
import pwd
from pathlib import Path

from dylathleen.api.abstract.model import CustomSoftwareAction, SoftwarePackage
from dylathleen.commons.app_launch import LaunchablePackage
from dylathleen.commons.repository import repository_publisher
from dylathleen.gems.bin.registry import BinError, expanded_path

ICON = str(Path(__file__).with_name('resources') / 'img' / 'bin.svg')


class BinApplication(LaunchablePackage, SoftwarePackage):
    def __init__(self, key, entry, system, i18n):
        self.system = system
        self.entry = entry
        self.key = key
        self.scope = 'system' if system else 'user'
        try:
            root_home = pwd.getpwuid(0).pw_dir
            path = key.replace('$HOME/', f'{root_home}/', 1).replace('${HOME}/', f'{root_home}/', 1) if system else key
            self.path = path if system else str(expanded_path(path))
        except BinError:
            self.path = key
        self.source = entry.get('url') or ''
        self.pinned = bool(entry.get('pinned'))
        self.update_error = None
        self.i18n = i18n
        description = i18n[f'bin.scope.{self.scope}']
        if self.pinned:
            description += f" · {i18n['bin.pinned']}"
        version = entry.get('version')
        super().__init__(id=f'{self.scope}:{key}', name=Path(self.path).name, version=version,
                         latest_version=version, description=description, installed=True)

    def get_type(self):
        return 'bin'

    def get_type_icon_path(self):
        return ICON

    def get_default_icon_path(self):
        return ICON

    def has_info(self):
        return True

    def has_history(self):
        return False

    def can_be_downgraded(self):
        return False

    def is_application(self):
        return True

    def supports_disk_cache(self):
        return False

    def get_disk_cache_path(self):
        return None

    def get_data_to_cache(self):
        return {}

    def fill_cached_data(self, data):
        pass

    def supports_backup(self):
        return False

    def get_publisher(self):
        return repository_publisher((self.source,), self.entry.get('provider'))

    def get_name_tooltip(self):
        return f'{self.name} ({self.description})\n{self.path}'

    def get_update_tip(self):
        return self.update_error

    def get_custom_actions(self):
        verb = 'unpin' if self.pinned else 'pin'
        key = f'bin.action.{verb}'
        return (self.configure_launch_action(),
                CustomSoftwareAction(key, key, ICON, verb, requires_root=False, requires_confirmation=False))

    def executable(self):
        return os.path.isabs(self.path) and os.path.isfile(self.path) and os.access(self.path, os.X_OK)
