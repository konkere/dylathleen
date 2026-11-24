from pathlib import Path
from urllib.parse import urlsplit

from dylathleen.api.abstract.model import CustomSoftwareAction, SoftwarePackage

ICON = str(Path(__file__).with_name('resources') / 'img' / 'debget.svg')


class DebGetApplication(SoftwarePackage):
    def __init__(self, name, recipe, state, registered_method, available, actions, compatible=True):
        self.recipe = recipe
        self.deb_name = name
        self.registered_method = registered_method
        self.method = registered_method or (recipe.method if recipe else None)
        self.state = state[1] if state else ''
        self.actions = actions
        self.compatible = compatible
        version = state[0] if state else None
        fields = recipe.fields if recipe else {}
        super().__init__(id=name, name=fields.get('PRETTY_NAME') or name, version=version,
                         description=fields.get('SUMMARY', ''), latest_version=available or version,
                         installed=self.state == 'installed', update=self.state == 'installed' and available is not None,
                         categories=[f'debget-{self.method or "direct"}', 'debget-local' if recipe and recipe.repo == '99-local'
                                     else 'debget-official' if recipe and recipe.repo in {'01-main', '00-builtin'}
                                     else 'debget-external'])

    def get_type(self):
        return 'debget'

    def get_default_icon_path(self):
        return ICON

    def get_type_icon_path(self):
        return ICON

    def has_info(self):
        return True

    def has_history(self):
        return False

    def is_application(self):
        return True

    def can_be_downgraded(self):
        return False

    def can_be_uninstalled(self):
        return self.registered_method is not None and self.installed

    def can_be_installed(self):
        return self.recipe is not None and not self.recipe.deprecated and self.compatible and not self.installed

    def can_be_run(self):
        return False

    def supports_disk_cache(self):
        return False

    def get_disk_cache_path(self):
        return None

    def get_data_to_cache(self):
        return {}

    def fill_cached_data(self, data):
        pass

    def get_publisher(self):
        if self.recipe:
            try:
                return urlsplit(self.recipe.fields.get('WEBSITE', '')).hostname
            except ValueError:
                return None

    def supports_backup(self):
        return False

    def get_custom_actions(self):
        if self.recipe:
            yield self.actions['resolve']
        if self.registered_method and self.recipe:
            yield self.actions['reinstall']
            yield self.actions['purge']
            if self.registered_method in {'apt', 'ppa'} and self.installed:
                yield self.actions['upgrade_one']


def action(name, online=False, refresh=True):
    key = 'debget.action.' + name
    return CustomSoftwareAction(key, key, ICON, name, requires_root=False, requires_internet=online,
                                requires_confirmation=False, refresh=refresh)
