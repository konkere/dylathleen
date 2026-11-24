from importlib import import_module
import os
from logging import Logger
from typing import List, Generator

from dylathleen import __app_name__, ROOT_DIR
from dylathleen.api.abstract.controller import SoftwareManager, ApplicationContext
from dylathleen.view.util import translation

FORBIDDEN_GEMS_FILE = f'/etc/{__app_name__}/gems.forbidden'


SUPPORTED = {
    'appimage': 'AppImageManager',
    'bin': 'BinManager',
    'flatpak': 'FlatpakManager',
    'snap': 'SnapManager',
}


def read_forbidden_gems() -> Generator[str, None, None]:
    try:
        with open(FORBIDDEN_GEMS_FILE) as f:
            forbidden_lines = f.readlines()

        for line in forbidden_lines:
            clean_line = line.strip()

            if clean_line and not clean_line.startswith('#'):
                yield clean_line

    except FileNotFoundError:
        pass


def load_managers(locale: str, context: ApplicationContext, config: dict, default_locale: str, logger: Logger) -> List[SoftwareManager]:
    managers = []

    forbidden_gems = {gem for gem in read_forbidden_gems()}

    for name, class_name in SUPPORTED.items():
        if name in forbidden_gems:
            continue
        module = import_module(f'dylathleen.gems.{name}.controller')
        if locale:
            directory = os.path.join(ROOT_DIR, 'gems', name, 'resources', 'locale')
            context.i18n.current.update(translation.get_locale_keys(locale, directory)[1])
            if default_locale and context.i18n.default:
                context.i18n.default.update(translation.get_locale_keys(default_locale, directory)[1])
        manager = getattr(module, class_name)(context=context)
        enabled = config['gems']
        manager.set_enabled(manager.is_default_enabled() if enabled is None else name in enabled)
        managers.append(manager)
    return managers
