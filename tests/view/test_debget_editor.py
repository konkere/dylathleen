from logging import getLogger
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication, QCheckBox, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QWidget

from dylathleen.api.abstract.view import FormComponent
from dylathleen.gems.debget.controller import DebGetManager
from dylathleen.gems.debget.editor import BashEdit, RecipeEditor
from dylathleen.gems.debget.editor_model import RecipeEditorComponent
from dylathleen.gems.debget.generator import Asset
from dylathleen.gems.debget.model import ICON
from dylathleen.view.qt.components import to_widget
from dylathleen.view.qt.dialog import ConfirmationDialog
from dylathleen.view.util.translation import I18n, get_locale_keys


class DebGetEditorTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(['test', '-platform', 'offscreen'])

    def setUp(self):
        locale = Path(ICON).parents[1] / 'locale'
        ru = {**get_locale_keys('ru')[1], **get_locale_keys('ru', str(locale))[1]}
        self.i18n = I18n('ru', ru, 'en', {})
        self.manager = DebGetManager(SimpleNamespace(i18n=self.i18n, logger=getLogger(__name__)))

    def show_confirmation(self, title, body, components, **kwargs):
        widgets = [to_widget(component, self.i18n) for component in components]
        self.dialog = ConfirmationDialog(title, body, self.i18n, widgets=widgets, **kwargs)
        self.addCleanup(self.dialog.close)
        self.dialog.show()
        self.qt.processEvents()
        return False

    def test_warning_is_always_visible_and_editor_updates_model(self):
        model = RecipeEditorComponent('DEFVER=1', 'ВНИМАНИЕ! Исполняемый код', 'Проверьте рецепт.')
        widget = to_widget(model, I18n('ru', {}, 'en', {}))
        self.assertIsInstance(widget, RecipeEditor)
        self.addCleanup(widget.close)
        widget.show()
        self.qt.processEvents()
        editor = widget.findChild(BashEdit)
        warnings = [label for label in widget.findChildren(QLabel) if label.text().startswith('ВНИМАНИЕ')]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].isVisible())
        self.assertLess(warnings[0].mapTo(widget, warnings[0].rect().topLeft()).y(), editor.y())
        self.assertGreater(editor.gutter.width(), 0)
        editor.setPlainText('DEFVER=2\n# updated')
        self.assertEqual(model.value, 'DEFVER=2\n# updated')

    def test_editor_and_preview_scroll_only_inside_bash_field(self):
        self.request_confirmation = self.show_confirmation
        source = '# a recipe line\n' * 100
        actions = self.manager._combo('Действие', [('Сохранить рецепт', 'save')])
        for components, body in (
                ([self.manager._editor(source), actions], None),
                ([FormComponent([self.manager._field('package', 'example')], spaces=False),
                  self.manager._editor(source), actions], None),
                ([self.manager._editor(source, True)], self.i18n['debget.switch'])):
            with self.subTest(count=len(components), body=body):
                self.manager._confirm(self, body, components)
                editor = self.dialog.findChild(BashEdit)
                self.assertIsNone(self.dialog.findChild(QScrollArea))
                self.assertGreater(editor.verticalScrollBar().maximum(), 0)
                self.assertEqual(editor.horizontalScrollBar().maximum(), 0)
                editor.verticalScrollBar().setValue(editor.verticalScrollBar().maximum())
                self.assertTrue(self.dialog.findChild(QWidget, 'debget_recipe_warning').isVisible())
                self.assertTrue(self.dialog.findChild(QPushButton, 'ok').isVisible())
                self.dialog.close()

    def test_log_uses_its_own_text_scrollbar(self):
        self.request_confirmation = self.show_confirmation
        self.manager._confirm(self, components=[self.manager._readonly('message\n' * 100)])
        self.assertIsNone(self.dialog.findChild(QScrollArea))
        field = self.dialog.findChild(QPlainTextEdit)
        self.assertTrue(field.isReadOnly())
        self.assertGreater(field.verticalScrollBar().maximum(), 0)

    def test_generator_keeps_last_asset_reachable_without_horizontal_scrolling(self):
        assets = [Asset(f'example-{"release" * 20}-{index}_amd64.deb', 'https://example.org/file', 'v1')
                  for index in range(100)]

        def next_confirmation(*args, components, **kwargs):
            if isinstance(components[0], FormComponent):
                components[0].components[0].set_value('https://github.com/owner/tool')
                return True
            return self.show_confirmation(*args, components=components, **kwargs)

        watcher = SimpleNamespace(request_confirmation=next_confirmation, change_substatus=lambda _: None)
        with patch('dylathleen.gems.debget.controller.client.user_token', return_value=''), \
                patch('dylathleen.gems.debget.controller.generator.Forge',
                      return_value=SimpleNamespace(analyse=lambda: ('', [assets]))):
            self.assertFalse(self.manager.generate(None, watcher))
        scroll = self.dialog.findChild(QScrollArea)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        last = scroll.widget().findChildren(QCheckBox)[-1]
        scroll.ensureWidgetVisible(last)
        self.assertTrue(last.visibleRegion().boundingRect().isValid())
