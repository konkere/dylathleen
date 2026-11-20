import os
import shutil
import subprocess
import sys
import traceback
from typing import List, Tuple

from PyQt5.QtCore import QCoreApplication, QSize
from PyQt5.QtGui import QIcon, QIconEngine
from colorama import Fore

from dylathleen import __app_name__
from dylathleen.api.abstract.controller import SoftwareManager
from dylathleen.api.paths import CONFIG_DIR, CACHE_DIR, TEMP_DIR
from dylathleen.commons.system import run_cmd, notify_user as send_notification
from dylathleen.view.util import resource


class _TwilightIconEngine(QIconEngine):
    def __init__(self):
        super().__init__()
        self.main = QIcon(resource.get_path('img/logo.svg'))
        self.small = QIcon(resource.get_path('img/dylathleen-twilight-small.svg'))

    def clone(self):
        return _TwilightIconEngine()

    def availableSizes(self, mode, state):
        # X11 uses these sizes for the window icon it sends to the panel.
        return [QSize(size, size) for size in (16, 24, 32, 48, 128)]

    def _icon(self, size):
        return self.small if min(size.width(), size.height()) <= 32 else self.main

    def pixmap(self, size, mode, state):
        return self._icon(size).pixmap(size, mode, state)

    def paint(self, painter, rect, mode, state):
        self._icon(rect.size()).paint(painter, rect, mode=mode, state=state)


def notify_user(msg: str, icon_path: str = None):
    icon_id = icon_path

    if not icon_id:
        icon_id = get_default_icon()[0]

    send_notification(msg, __app_name__, icon_id)


def get_default_icon(system: bool = True) -> Tuple[str, QIcon]:
    path = resource.get_path('img/logo.svg')
    # Keep the theme name for notifications, but choose the artwork in Qt by size.
    if system and QIcon.hasThemeIcon(__app_name__):
        path = __app_name__
    return path, QIcon(_TwilightIconEngine())


def restart_app():
    appimage_path = os.getenv('APPIMAGE')

    restart_cmd = [appimage_path] if appimage_path else [sys.executable, *sys.argv]

    subprocess.Popen(restart_cmd)
    QCoreApplication.exit()


def get_distro():
    if os.path.exists('/etc/arch-release'):
        return 'arch'

    if os.path.exists('/etc/os-release'):
        with open('/etc/os-release', 'r') as os_release_file:
            for line in os_release_file:
                if 'ID_LIKE=arch' in line:
                    return 'arch'

    if os.path.exists('/proc/version'):
        if 'ubuntu' in run_cmd('cat /proc/version').lower():
            return 'ubuntu'

    return 'unknown'


def clean_app_files(managers: List[SoftwareManager], logs: bool = True):

    if logs:
        print('[dylathleen] Cleaning configuration and cache files')

    for path in (CACHE_DIR, CONFIG_DIR, TEMP_DIR):
        if logs:
            print('[dylathleen] Deleting directory {}'.format(path))

        if os.path.exists(path):
            try:
                shutil.rmtree(path)
                if logs:
                    print('{}[dylathleen] Directory {} deleted{}'.format(Fore.YELLOW, path, Fore.RESET))
            except Exception:
                if logs:
                    print('{}[dylathleen] An exception has happened when deleting {}{}'.format(Fore.RED, path, Fore.RESET))
                    traceback.print_exc()

    if managers:
        for m in managers:
            m.clear_data()

    if logs:
        print('[dylathleen] Cleaning finished')
