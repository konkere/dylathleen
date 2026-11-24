"""Run from outside the checkout against the installed package, as a desktop user."""
import argparse
import logging
import sys

from PyQt5.QtCore import QTimer

from dylathleen.manage import new_manage_panel
from dylathleen.view.core.config import CoreConfigManager

args = argparse.Namespace(offline=True, suggestions=False, settings=False, reset=False)
app, panel = new_manage_panel(args, CoreConfigManager().get_config(), logging.getLogger('smoke'))
formats = {type(m).__module__.split('.')[2] for m in panel.manager.managers}
assert formats == {'flatpak', 'snap', 'appimage'}, formats
panel.show()


def check():
    window = panel.manage_window
    if window.isVisible() and window.windowTitle() == 'Dylath-Leen' and not window.first_refresh and not window.thread_refresh.isRunning():
        print('Main window ready', flush=True)
        app.exit(0)


timer = QTimer()
timer.timeout.connect(check)
timer.start(100)
QTimer.singleShot(45000, lambda: app.exit(1))
sys.exit(app.exec_())
