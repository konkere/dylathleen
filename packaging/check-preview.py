"""Check both Qt presentation paths with a small screen and Russian labels."""
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

from dylathleen.api.abstract.controller import UpgradeRequirement
from dylathleen.gems.flatpak.model import FlatpakApplication
from dylathleen.view.qt.apps_table import PackagesTable
from dylathleen.view.qt.thread import UpgradeSelected
from dylathleen.view.qt.view_model import PackageView
from dylathleen.view.util.translation import I18n, get_locale_keys

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

# Keep unknown, exact and upper-bound sizes distinct in rows and group totals.
dialog.i18n = I18n(*get_locale_keys('ru'), *get_locale_keys('en'))
bounded = UpgradeRequirement(pkg, required_size=1_000_001, download_is_upper_bound=True)
exact = UpgradeRequirement(pkg, required_size=2_000_000)
unknown = UpgradeRequirement(pkg)
assert 'Загрузка: ≤ 1.01 MB' in dialog._req_as_option(bounded).label
assert dialog._download_label([bounded, exact]) == '≤ 3.01 MB'
assert dialog._download_label([bounded, unknown]) == '≤ 1.01 MB для 1 из 2'
assert dialog._download_label([unknown]) == 'неизвестно'
assert dialog._storage_label([bounded, exact]) == 'неизвестно'
assert dialog._download_label([UpgradeRequirement(pkg, required_size=0)]) == '0'
assert 'Количество: 2' in dialog._download_form([bounded, exact], 'action.update.label_to_upgrade').label
