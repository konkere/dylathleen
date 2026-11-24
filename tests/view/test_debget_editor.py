from unittest import TestCase

from PyQt5.QtWidgets import QApplication, QLabel

from dylathleen.gems.debget.editor import BashEdit, RecipeEditor
from dylathleen.gems.debget.editor_model import RecipeEditorComponent
from dylathleen.view.qt.components import to_widget
from dylathleen.view.util.translation import I18n


class DebGetEditorTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(['test', '-platform', 'offscreen'])

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
