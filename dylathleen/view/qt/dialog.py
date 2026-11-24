from typing import List, Optional

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QIcon, QCursor
from PyQt5.QtWidgets import QApplication, QMessageBox, QLabel, QWidget, QHBoxLayout, QDialog, QVBoxLayout, QSizePolicy, QPushButton, \
    QScrollArea, QFrame, QCheckBox

from dylathleen.api.abstract.view import MessageType
from dylathleen.view.qt.components import new_spacer
from dylathleen.view.util.translation import I18n

MSG_TYPE_MAP = {
    MessageType.ERROR: QMessageBox.Critical,
    MessageType.INFO: QMessageBox.Information,
    MessageType.WARNING: QMessageBox.Warning
}


def show_message(title: str, body: str, type_: MessageType, icon: Optional[QIcon] = None):
    popup = QMessageBox()
    popup.setWindowTitle(title)
    popup.setText(body)
    popup.setIcon(MSG_TYPE_MAP[type_])

    if icon:
        popup.setWindowIcon(icon)

    popup.exec_()


class ConfirmationDialog(QDialog):

    def __init__(self, title: str, body: Optional[str], i18n: I18n, icon: Optional[QIcon] = None,
                 widgets: Optional[List[QWidget]] = None, confirmation_button: bool = True, deny_button: bool = True,
                 window_cancel: bool = False, confirmation_label: Optional[str] = None, deny_label: Optional[str] = None,
                 confirmation_icon: bool = True, min_width: Optional[int] = None,
                 min_height: Optional[int] = None, max_width: Optional[int] = None,
                 confirmation_icon_type: MessageType = MessageType.INFO, count_selected: bool = False,
                 scroll_content: bool = True):
        super(ConfirmationDialog, self).__init__()

        if not window_cancel:
            self.setWindowFlags(Qt.CustomizeWindowHint | Qt.WindowTitleHint)

        self.setLayout(QVBoxLayout())
        self.setWindowTitle(title)
        self.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        self.setMinimumWidth(min_width if min_width and min_width > 0 else 250)

        if max_width is not None and max_width > 0:
            self.setMaximumWidth(max_width)

        if isinstance(min_height, int) and min_height > 0:
            self.setMinimumHeight(min_height)

        self.confirmed = False

        if icon:
            self.setWindowIcon(icon)

        container_body = QWidget()
        container_body.setObjectName('confirm_container_body')
        container_body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        scroll = None
        if widgets:
            container_body.setLayout(QVBoxLayout())
            if scroll_content:
                scroll = QScrollArea(self)
                scroll.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
                scroll.setFrameShape(QFrame.NoFrame)
                scroll.setWidgetResizable(True)
                scroll.setWidget(container_body)
                self.layout().addWidget(scroll)
            else:
                self.layout().addWidget(container_body, 1)
        else:
            container_body.setLayout(QHBoxLayout())
            self.layout().addWidget(container_body)

            if confirmation_icon:
                lb_icon = QLabel()
                lb_icon.setObjectName("confirm_dialog_icon")
                lb_icon.setProperty("type", confirmation_icon_type.name.lower())
                container_body.layout().addWidget(lb_icon)

        if body:
            lb_msg = QLabel(body)
            lb_msg.setObjectName('confirm_msg')
            lb_msg.setWordWrap(True)
            container_body.layout().addWidget(lb_msg)

        if widgets:
            for w in widgets:
                container_body.layout().addWidget(w)
        else:
            container_body.layout().addWidget(new_spacer())

        container_bottom = QWidget()
        container_bottom.setObjectName('confirm_container_bottom')
        container_bottom.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        container_bottom.setLayout(QHBoxLayout())
        self.layout().addWidget(container_bottom)

        container_bottom.layout().addWidget(new_spacer())

        if confirmation_button:
            bt_confirm = QPushButton(confirmation_label.capitalize() if confirmation_label else i18n['popup.button.yes'])
            bt_confirm.setObjectName('ok')
            bt_confirm.setCursor(QCursor(Qt.PointingHandCursor))
            bt_confirm.setDefault(True)
            bt_confirm.setAutoDefault(True)
            if count_selected:
                boxes = container_body.findChildren(QCheckBox)
                label = bt_confirm.text()

                def update_count():
                    count = sum(box.isChecked() for box in boxes)
                    bt_confirm.setText(f'{label} ({count})')
                    bt_confirm.setEnabled(count > 0)

                for box in boxes:
                    box.toggled.connect(update_count)
                update_count()

            bt_confirm.clicked.connect(self.confirm)
            container_bottom.layout().addWidget(bt_confirm)

        if deny_button:
            bt_cancel = QPushButton(deny_label.capitalize() if deny_label else i18n['popup.button.no'])
            bt_cancel.setObjectName('bt_cancel')
            bt_cancel.setCursor(QCursor(Qt.PointingHandCursor))
            bt_cancel.clicked.connect(self.close)
            container_bottom.layout().addWidget(bt_cancel)

            if not confirmation_button:
                bt_cancel.setDefault(True)
                bt_cancel.setAutoDefault(True)

        if widgets:
            self.ensurePolished()
            screen_size = QApplication.desktop().availableGeometry(QCursor.pos()).size()
            available = (screen_size - QSize(32, 64)).boundedTo(self.maximumSize())
        if scroll:
            # A scroll area's size hint can be smaller than even a short form.
            # Fit the form initially and keep scrolling for oversized content.
            body_size = container_body.sizeHint().expandedTo(container_body.minimumSizeHint())
            extra = self.layout().sizeHint() - scroll.sizeHint()
            size = (body_size + extra).expandedTo(self.sizeHint()).boundedTo(available)
            body_width = max(0, size.width() - extra.width())
            wrapped_height = container_body.layout().totalHeightForWidth(body_width)
            required_height = max(body_size.height(), wrapped_height) + extra.height()
            if required_height > available.height():
                # A vertical bar reduces the viewport's width. Reserve that
                # space so a tall list does not gain a horizontal bar as well.
                bar_width = scroll.verticalScrollBar().sizeHint().width()
                size.setWidth(min(available.width(), size.width() + bar_width))
            size.setHeight(min(available.height(), max(size.height(), required_height)))
            self.resize(size)
        elif widgets:
            self.resize(self.sizeHint().boundedTo(available))

    def confirm(self):
        self.confirmed = True
        self.close()

    def ask(self) -> bool:
        self.exec_()
        return self.confirmed


def ask_confirmation(title: str, body: str, i18n: I18n, icon: Optional[QIcon] = None,
                     widgets: List[QWidget] = None) -> bool:
    popup = ConfirmationDialog(title=title, body=body, i18n=i18n, icon=icon, widgets=widgets)
    popup.exec_()
    return popup.confirmed
