from unittest import TestCase

from PyQt5.QtWidgets import QApplication, QScrollArea, QWidget

from dylathleen.api.abstract.view import FormComponent, InputOption, SelectViewType, SingleSelectComponent, TextInputComponent
from dylathleen.view.qt.components import to_widget
from dylathleen.view.qt.dialog import ConfirmationDialog
from dylathleen.view.util.translation import I18n, get_locale_keys


class ConfirmationDialogTest(TestCase):

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(['test', '-platform', 'offscreen'])

    def _show(self, widgets, body=None):
        i18n = I18n('ru', get_locale_keys('ru')[1], 'en', get_locale_keys('en')[1])
        rendered = [to_widget(widget, i18n) if isinstance(widget, FormComponent) else widget for widget in widgets]
        dialog = ConfirmationDialog('Релизы bin', body, i18n, widgets=rendered, window_cancel=True)
        self.addCleanup(dialog.close)
        dialog.show()
        self.qt.processEvents()
        return dialog.findChild(QScrollArea)

    def test_short_install_forms_fit_without_scrolling(self):
        release = InputOption('v0.29.4', 'v0.29.4')
        scope = InputOption('Пользовательская установка (~/.local/bin)', 'user')
        asset = InputOption('example-v0.29.4-x86_64-unknown-linux-gnu.tar.gz', 'asset')
        for components, body in (
                ([TextInputComponent('URL репозитория', min_width=440)], None),
                ([SingleSelectComponent(SelectViewType.COMBO, 'Релиз', [release], release),
                  SingleSelectComponent(SelectViewType.COMBO, 'Область установки', [scope], scope),
                  TextInputComponent('Имя установленного файла', value='example')], None),
                ([SingleSelectComponent(SelectViewType.COMBO, 'Файл релиза', [asset], asset)],
                 'Выберите файл для example v0.29.4')):
            with self.subTest(fields=len(components), body=body):
                scroll = self._show([FormComponent(components)], body)
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                self.assertEqual(scroll.verticalScrollBar().maximum(), 0)

    def test_content_taller_than_the_screen_is_scrollable(self):
        widget = QWidget()
        widget.setMinimumHeight(self.qt.primaryScreen().availableGeometry().height() * 2)
        scroll = self._show([widget])
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
