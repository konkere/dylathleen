"""Check both Qt presentation paths with a small screen and Russian labels."""
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

from dylathleen.api.abstract.controller import UpgradeRequirement
from dylathleen.gems.flatpak.model import FlatpakApplication
from dylathleen.view.qt.apps_table import PackagesTable
from dylathleen.view.qt.thread import UpgradeSelected
from dylathleen.view.qt.view_model import PackageView
from dylathleen.view.util.translation import I18n

app = QApplication([])
owner = QWidget()
owner.i18n = I18n('ru', {'flatpak.update.build': 'обновление сборки',
                         'flatpak.update.unknown': 'целевая версия неизвестна'}, 'en', {})
table = PackagesTable(owner, None, False, None)
table.setRowCount(1)
dialog = UpgradeSelected(None, None, owner.i18n, owner)
for installed, target, expected in [
        ('3.2.4', '3.2.8', '3.2.4 → 3.2.8'),
        ('4.13.2', '4.13.2', '4.13.2 · обновление сборки (aaaa → bbbb)'),
        ('3.2.4', None, '3.2.4 → целевая версия неизвестна')]:
    pkg = FlatpakApplication(id='org.example.Test', name='Test', installed=True, update=True,
                             version=installed, latest_version=target, commit='aaaa', target_commit='bbbb')
    view = PackageView(pkg, owner.i18n)
    view.table_index = 0
    table._set_col_version(0, view, 600)
    assert table.cellWidget(0, 0).findChild(QLabel).text() == expected
    option = dialog._req_as_option(UpgradeRequirement(pkg), display_sizes=False)
    assert expected in option.label
    print(expected)
