import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from PyQt5.QtCore import QPoint
from PyQt5.QtGui import QContextMenuEvent
from PyQt5.QtWidgets import QApplication, QComboBox, QToolButton, QWidget

from dylathleen.commons.app_launch import ApplicationLauncher, LaunchProfile, LaunchProfiles
from dylathleen.commons.desktop_entry import DesktopCatalogue
from dylathleen.gems.bin.model import BinApplication
from dylathleen.gems.debget.model import DebGetApplication
from dylathleen.view.qt.apps_table import PackagesTable
from dylathleen.view.qt.components import FormQt
from dylathleen.view.qt.view_model import PackageView
from dylathleen.view.util.translation import I18n, get_locale_keys


class LaunchViewTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(['test', '-platform', 'offscreen'])

    def setUp(self):
        self.i18n = I18n('ru', get_locale_keys('ru')[1], 'en', get_locale_keys('en')[1])
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.launcher = ApplicationLauncher(self.i18n)
        self.launcher.profiles = LaunchProfiles(self.root / 'launch.json')

    def program(self, name):
        program = self.root / name
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o755)
        return str(program)

    def test_configure_prefills_bin_absolute_path_and_stores_mode(self):
        program = self.program('bin-example')
        pkg = BinApplication(program, {'version': '1'}, False, self.i18n)
        self.launcher.populate(pkg, DesktopCatalogue(()), {}, managed_path=program)

        def confirm(components, **kwargs):
            self.assertFalse(kwargs['scroll_content'])
            mode, path, args = components[0].components
            self.assertEqual(path.get_value(), program)
            mode.value = next(option for option in mode.options if option.value == 'gui')
            args.set_value('--message "two words"')
            return True

        watcher = SimpleNamespace(request_confirmation=confirm)
        self.assertTrue(self.launcher.configure(pkg, watcher))
        self.assertTrue(pkg.can_be_run())
        self.assertEqual(pkg.launch_state.command.argv, (program, '--message', 'two words'))
        saved = self.launcher.profiles.profile(self.launcher.profiles.read(), self.launcher.key(pkg))
        self.assertEqual(saved.mode, 'gui')

    def test_debget_editable_combo_accepts_candidate_or_custom_path(self):
        program, helper, custom = map(self.program, ('app', 'helper', 'my app'))
        pkg = DebGetApplication('sample-app', None, ('1', 'installed'), 'github', None, {})
        self.launcher.populate(pkg, DesktopCatalogue(()), {}, files=(program, helper))
        self.assertEqual([action.manager_method for action in pkg.get_custom_actions()], ['configure_launch'])

        def confirm(components, **kwargs):
            mode, path, _ = components[0].components
            mode.value = next(option for option in mode.options if option.value == 'gui')
            form = FormQt(components[0], self.i18n)
            combo = next(box for box in form.findChildren(QComboBox) if box.count() == 2)
            self.assertTrue(combo.isEditable())
            self.assertEqual([combo.itemText(0), combo.itemText(1)], [program, helper])
            combo.setCurrentIndex(1)
            self.assertEqual(path.get_value(), helper)
            combo.lineEdit().setText(custom)
            self.assertEqual(path.get_value(), custom)
            return True

        self.assertTrue(self.launcher.configure(pkg, SimpleNamespace(request_confirmation=confirm)))
        self.assertEqual(pkg.launch_state.command.executable, custom)

    def test_run_tooltip_and_context_menu_then_enable_run(self):
        parent = QWidget()
        parent.i18n = self.i18n
        table = PackagesTable(parent, None, False, logging.getLogger(__name__))
        program = self.program('cli')
        pkg = BinApplication(program, {'version': '1'}, False, self.i18n)
        self.launcher.populate(pkg, DesktopCatalogue(()), {}, managed_path=program)
        view = PackageView(pkg, self.i18n)
        view.table_index = 0
        table.setRowCount(1)
        column = table.ACTIONS_COLUMN
        table._set_col_actions(column, view)
        button = table.cellWidget(0, column).findChild(QToolButton, 'app_run')
        self.assertFalse(button.isEnabled())
        self.assertEqual(button.toolTip(), self.i18n['launch.unknown'])
        local = QPoint(0, 0)
        event = QContextMenuEvent(QContextMenuEvent.Mouse, local, QPoint(10, 10))
        with patch.object(table, 'show_pkg_actions') as menu:
            self.qt.sendEvent(table.viewport(), event)
        menu.assert_called_once_with(view, QPoint(10, 10))
        self.launcher.profiles.save(self.launcher.key(pkg), LaunchProfile(mode='gui'))
        self.launcher.populate(pkg, DesktopCatalogue(()), self.launcher.profiles.read(), managed_path=program)
        table._set_col_actions(column, view)
        button = table.cellWidget(0, column).findChild(QToolButton, 'app_run')
        self.assertTrue(button.isEnabled())
