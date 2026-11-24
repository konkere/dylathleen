import re

from PyQt5.QtCore import QRect, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPainter, QSyntaxHighlighter, QTextCharFormat
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget


class BashHighlight(QSyntaxHighlighter):
    def highlightBlock(self, text):
        for pattern, color in (
            (r"\b(?:if|then|elif|else|fi|case|esac|function|local|for|do|done|while|return)\b", '#a158d1'),
            (r'"(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27', '#438852'),
            (r'\$\{[^}]+\}|\$\w+', '#327caf'),
            (r'(?:^|\s)#[^\n]*', '#8b8383'),
        ):
            style = QTextCharFormat()
            style.setForeground(QColor(color))
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), len(match[0]), style)


class Gutter(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        editor = self.editor
        painter = QPainter(self)
        painter.fillRect(event.rect(), editor.palette().alternateBase())
        painter.setPen(editor.palette().text().color())
        block = editor.firstVisibleBlock()
        top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
        while block.isValid() and top <= event.rect().bottom():
            height = editor.blockBoundingRect(block).height()
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(0, round(top), self.width() - 5, editor.fontMetrics().height(),
                                 Qt.AlignRight, str(block.blockNumber() + 1))
            block = block.next()
            top += height


class BashEdit(QPlainTextEdit):
    def __init__(self, model):
        super().__init__(model.value)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setMinimumSize(570, 300)
        self.setReadOnly(model.readonly)
        self.gutter = Gutter(self)
        self.highlight = BashHighlight(self.document())
        self.blockCountChanged.connect(self.update_gutter_width)
        self.updateRequest.connect(self.update_gutter)
        self.textChanged.connect(lambda: setattr(model, 'value', self.toPlainText()))
        self.update_gutter_width()

    def gutter_width(self):
        return 11 + self.fontMetrics().horizontalAdvance('9') * len(str(self.blockCount()))

    def update_gutter_width(self):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        area = self.contentsRect()
        self.gutter.setGeometry(QRect(area.left(), area.top(), self.gutter_width(), area.height()))


class RecipeEditor(QWidget):
    def __init__(self, model):
        super().__init__()
        layout = QVBoxLayout(self)
        banner = QWidget()
        banner.setObjectName('debget_recipe_warning')
        banner.setStyleSheet('#debget_recipe_warning { background: #fff0f0; border: 2px solid #b52727; }'
                             '#debget_recipe_warning QLabel { color: #851919; }')
        row = QHBoxLayout(banner)
        icon = QLabel('⚠')
        font = QFont()
        font.setPixelSize(25)
        icon.setFont(font)
        icon.setAlignment(Qt.AlignTop)
        row.addWidget(icon)
        message = QLabel()
        message.setTextFormat(Qt.PlainText)
        message.setText(model.warning_title + '\n' + model.warning)
        message.setWordWrap(True)
        row.addWidget(message, 1)
        layout.addWidget(banner)
        layout.addWidget(BashEdit(model), 1)
