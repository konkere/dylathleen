import subprocess
from collections.abc import Iterable
from subprocess import Popen

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QCursor
from PyQt5.QtWidgets import QApplication, QToolButton, QDialog, QVBoxLayout, QGroupBox, \
    QLineEdit, QLabel, QGridLayout, QPushButton, QPlainTextEdit, QScrollArea, QFrame, QWidget, QSizePolicy, \
    QHBoxLayout

from dylathleen.api.abstract.cache import MemoryCache
from dylathleen.commons.installed_files import InstalledFiles
from dylathleen.commons.regex import RE_URL
from dylathleen.view.qt.components import new_spacer
from dylathleen.view.qt.qt_utils import get_current_screen_geometry
from dylathleen.view.util import resource
from dylathleen.view.util.translation import I18n

IGNORED_ATTRS = {'name', '__app__'}


class InfoDialog(QDialog):

    def __init__(self, pkg_info: dict, icon_cache: MemoryCache, i18n: I18n, can_open_url: bool):
        super(InfoDialog, self).__init__()
        self.setWindowTitle(str(pkg_info['__app__']))
        self.i18n = i18n
        self._can_open_url = can_open_url
        layout = QVBoxLayout()
        self.setLayout(layout)

        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        comps_container = QWidget()
        comps_container.setObjectName('root_container')
        comps_container.setLayout(QVBoxLayout())
        scroll.setWidget(comps_container)

        # shows complete field string
        self.text_field = QPlainTextEdit()
        self.text_field.setObjectName('full_field')
        self.text_field.setReadOnly(True)
        comps_container.layout().addWidget(self.text_field)
        self.text_field.hide()

        self.gbox_info = QGroupBox()
        self.gbox_info.setObjectName('fields')
        self.gbox_info.setLayout(QGridLayout())

        comps_container.layout().addWidget(self.gbox_info)

        # THERE ARE CRASHES WITH SOME RARE ICONS ( like insomnia ). IT CAN BE A QT BUG. IN THE MEANTIME, ONLY THE TYPE ICON WILL BE RENDERED
        #
        # icon_data = icon_cache.get(app['__app__'].model.icon_url)
        #
        # if icon_data and icon_data.get('icon'):
        #     self.setWindowIcon(icon_data.get('icon'))
        self.setWindowIcon(QIcon(pkg_info['__app__'].model.get_type_icon_path()))

        self.show_icon = QIcon(resource.get_path('img/show-field.svg'))
        self.open_icon = QIcon(resource.get_path('img/open-field.svg'))
        self.copy_icon = QIcon(resource.get_path('img/copy-field.svg'))

        for idx, attr in enumerate(sorted(pkg_info.keys())):
            if attr not in IGNORED_ATTRS and pkg_info[attr] is not None:
                i18n_key = pkg_info['__app__'].model.gem_name + '.info.' + attr.lower()
                val = pkg_info[attr]

                is_file_list = isinstance(val, InstalledFiles)
                if is_file_list:
                    show_val = '\n'.join(val.paths)
                    val = val.location
                elif not isinstance(val, str) and isinstance(pkg_info[attr], Iterable):
                    val = ' '.join([str(e).strip() for e in pkg_info[attr] if e])
                    show_val = '\n'.join(['* ' + str(e).strip() for e in pkg_info[attr] if e])
                else:
                    val = str(pkg_info[attr]).strip()
                    show_val = val

                i18n_val = i18n.get(f"{i18n_key}.{val.lower()}")

                if i18n_val:
                    val = i18n_val
                    show_val = val

                text = QLineEdit()
                text.setObjectName('field_value')
                text.setToolTip(val if is_file_list else show_val)
                text.setText(val)
                text.setCursorPosition(0)
                text.setReadOnly(True)

                label = QLabel(i18n.get(i18n_key, i18n.get(attr.lower(), attr)).capitalize())
                label.setObjectName('field_name')

                self.gbox_info.layout().addWidget(label, idx, 0)
                self.gbox_info.layout().addWidget(text, idx, 1)

                self._add_field_actions(idx, label.text(), val, show_val)

        layout.addWidget(scroll)

        lower_container = QWidget()
        lower_container.setObjectName('lower_container')
        lower_container.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        lower_container.setLayout(QHBoxLayout())

        self.bt_back = QPushButton('< {}'.format(self.i18n['back'].capitalize()))
        self.bt_back.setObjectName('back')
        self.bt_back.setVisible(False)
        self.bt_back.setCursor(QCursor(Qt.PointingHandCursor))
        self.bt_back.clicked.connect(self.back_to_info)

        lower_container.layout().addWidget(self.bt_back)
        lower_container.layout().addWidget(new_spacer())

        self.bt_copy_close = QPushButton(self.copy_icon, self.i18n['manage_window.info.copy_close'])
        self.bt_copy_close.setCursor(Qt.PointingHandCursor)
        self.bt_copy_close.clicked.connect(self._copy_and_close)
        self.bt_copy_close.hide()
        lower_container.layout().addWidget(self.bt_copy_close)

        self.bt_close = QPushButton(self.i18n['close'].capitalize())
        self.bt_close.setObjectName('close')
        self.bt_close.setCursor(QCursor(Qt.PointingHandCursor))
        self.bt_close.clicked.connect(self.close)

        lower_container.layout().addWidget(self.bt_close)
        layout.addWidget(lower_container)
        self.setMinimumWidth(int(self.gbox_info.sizeHint().width() * 1.2))

        screen_height = get_current_screen_geometry().height()
        self.setMaximumHeight(int(screen_height * 0.8))
        self.adjustSize()

    @staticmethod
    def open_url(url: str):
        Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    def _show_full_field_val(self, val: str):
        self.gbox_info.hide()
        self.bt_back.setVisible(True)
        self.bt_copy_close.show()
        self.text_field.show()
        self.text_field.setPlainText(val)

    @staticmethod
    def _icon_button(icon: QIcon, action: str, field: str):
        button = QToolButton()
        button.setIcon(icon)
        button.setToolTip(action)
        button.setAccessibleName(f'{action} — {field}')
        button.setCursor(Qt.PointingHandCursor)
        return button

    def _add_field_actions(self, row: int, label: str, value: str, full_value: str):
        if self._can_open_url and RE_URL.match(full_value):
            show = self._icon_button(self.open_icon, self.i18n['manage_window.info.open_url'], label)
            show.clicked.connect(lambda: self.open_url(full_value))
        else:
            show = self._icon_button(self.show_icon, self.i18n['show'].capitalize(), label)
            show.clicked.connect(lambda: self._show_full_field_val(full_value))
        show.setObjectName('show')

        copy = self._icon_button(self.copy_icon, self.i18n['copy'].capitalize(), label)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(value))
        self.gbox_info.layout().addWidget(show, row, 2, Qt.AlignRight)
        self.gbox_info.layout().addWidget(copy, row, 3)

    def _copy_and_close(self):
        QApplication.clipboard().setText(self.text_field.toPlainText())
        self.close()

    def back_to_info(self):
        self.text_field.setPlainText("")
        self.text_field.hide()
        self.gbox_info.show()
        self.bt_back.setVisible(False)
        self.bt_copy_close.hide()
