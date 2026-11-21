"""Exercise paging and filtering through the desktop UI with a local package list."""
import argparse
import logging
import time
from unittest.mock import patch

from dylathleen.gems.flatpak.model import FlatpakApplication
from dylathleen.manage import new_manage_panel
from dylathleen.view.core.config import CoreConfigManager
from dylathleen.view.qt.apps_table import UpgradeToggleButton
from dylathleen.view.qt.dialog import ConfirmationDialog

config = CoreConfigManager().get_default_config()
config['locale'] = 'en'
config['download']['icons'] = False
args = argparse.Namespace(offline=True, suggestions=False, settings=False, reset=False)
app, panel = new_manage_panel(args, config, logging.getLogger('pagination'))
window = panel.manage_window
models = [FlatpakApplication(id=f'org.example.Test{i}', name=f'Application {i:02}',
                             installed=True, update=True, installation='user',
                             version='1', latest_version='2') for i in range(64)]
models[-1].name = 'Last substring match'
window.update_pkgs(models, as_installed=True)
assert window.label_displayed.text() == '1–50 of 64'
window.next_page.click()
assert window.label_displayed.text() == '51–64 of 64'
assert window.table_apps.rowCount() == 14 and window.pkgs[-1].model is models[-1]
window.table_apps.cellWidget(13, 8).findChild(UpgradeToggleButton).click()
assert not window.pkgs[-1].update_checked
window.previous_page.click()
assert window.label_displayed.text() == '1–50 of 64'
with patch.object(ConfirmationDialog, 'ask', return_value=True), patch.object(window.thread_update, 'start'):
    window.upgrade_selected()
    assert len(window.thread_update.pkgs) == 64
    assert sum(p.update_checked for p in window.thread_update.pkgs) == 63
    window._finish_action()
window.next_page.click()
assert not window.pkgs[-1].update_checked


def filter_name(text):
    window.input_name.set_text(text)
    window.input_name.search_button.click()
    deadline = time.monotonic() + 5
    while window.thread_apply_filters.isRunning():
        assert time.monotonic() < deadline
        app.processEvents()
    app.processEvents()


filter_name('STRING')
assert window.label_displayed.text() == '1–1 of 1' and window.pkgs[0].model is models[-1]
filter_name('missing')
assert window.table_apps.rowCount() == 0 and window.label_displayed.text() == '0'
filter_name('')
window.next_page.click()
window.update_pkgs(models[:50], as_installed=True, keep_filters=True)
assert window.page_index == 0 and window.label_displayed.text() == '1–50 of 50'
window.display_limit = 0
window.update_pkgs(models, as_installed=True)
assert window.table_apps.rowCount() == 64 and window.next_page.isHidden()
filter_name('STRING')
assert window.table_apps.rowCount() == 1
window.stop_notifying_package_states()
print('Pagination, substring matching, page bounds and cross-page update selection passed.')
