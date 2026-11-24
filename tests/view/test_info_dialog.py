from unittest import TestCase
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication, QToolButton

from dylathleen.commons.installed_files import InstalledFiles
from dylathleen.gems.flatpak.model import FlatpakApplication
from dylathleen.view.qt.info import InfoDialog
from dylathleen.view.qt.view_model import PackageView
from dylathleen.view.util.translation import I18n, get_locale_keys


class InfoDialogTest(TestCase):

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(['test', '-platform', 'offscreen'])

    def setUp(self):
        i18n = I18n('ru', get_locale_keys('ru')[1], 'en', get_locale_keys('en')[1])
        self.paths = ['/opt/My App/start', '/opt/My App/значок.png']
        self.command = "'/opt/My App/start' --open"
        self.source = 'https://github.com/marcosnils/bin/releases/tag/v0.29.4'
        self.dialog = InfoDialog({
            '__app__': PackageView(FlatpakApplication(name='Example'), i18n),
            'app_files': InstalledFiles(self.paths[0], self.paths),
            'launch_command': self.command,
            'source': self.source,
        }, None, i18n, True)
        self.dialog.show()
        self.addCleanup(self.dialog.close)
        self.grid = self.dialog.gbox_info.layout()

    def test_row_buttons_copy_their_own_display_value(self):
        for row, expected in ((1, self.paths[0]), (2, self.command)):
            copy = self.grid.itemAtPosition(row, 3).widget()
            self.assertFalse(copy.icon().pixmap(16, 16).isNull())
            copy.click()
            self.assertEqual(expected, self.qt.clipboard().text())
            self.assertTrue(self.dialog.gbox_info.isVisible())
            self.assertFalse(self.dialog.bt_copy_close.isVisible())

    def test_url_uses_an_accessible_icon_and_opens_the_full_address(self):
        open_url = self.grid.itemAtPosition(3, 2).widget()
        self.assertIsInstance(open_url, QToolButton)
        self.assertEqual(open_url.text(), '')
        self.assertEqual(open_url.toolTip(), 'Открыть')
        self.assertTrue(open_url.accessibleName().startswith('Открыть'))
        self.assertFalse(open_url.icon().pixmap(16, 16).isNull())
        with patch.object(self.dialog, 'open_url') as action:
            open_url.click()
        action.assert_called_once_with(self.source)
        self.grid.itemAtPosition(3, 3).widget().click()
        self.assertEqual(self.qt.clipboard().text(), self.source)

    def test_copy_and_close_uses_the_entire_current_detail(self):
        self.grid.itemAtPosition(2, 2).widget().click()
        self.assertEqual(self.command, self.dialog.text_field.toPlainText())
        self.dialog.bt_back.click()
        self.assertTrue(self.dialog.bt_copy_close.isHidden())

        show = self.grid.itemAtPosition(1, 2).widget()
        self.assertFalse(show.icon().pixmap(16, 16).isNull())
        show.click()
        cursor = self.dialog.text_field.textCursor()
        cursor.select(cursor.WordUnderCursor)
        self.dialog.text_field.setTextCursor(cursor)
        self.assertTrue(self.dialog.bt_copy_close.isVisible())
        self.dialog.bt_copy_close.click()
        self.assertEqual('\n'.join(self.paths), self.qt.clipboard().text())
        self.assertFalse(self.dialog.isVisible())
