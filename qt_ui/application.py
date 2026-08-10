from __future__ import annotations

import sys
import ctypes
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QDialogButtonBox, QInputDialog,
    QLabel, QLayout, QMessageBox, QWidget,
)

from core.database import Database
from i18n import set_language
from .main_window import MainWindow


class _ComboBoxWheelBlocker(QObject):
    def eventFilter(self, watched, event) -> bool:
        if isinstance(watched, (QComboBox, QAbstractSpinBox)) and event.type() == QEvent.Type.Wheel:
            return True
        if event.type() == QEvent.Type.Show:
            if isinstance(watched, QAbstractSpinBox):
                # 長押し時は連続して値を変更し、細かな連打を不要にする。
                watched.setAccelerated(True)
            if isinstance(watched, QMessageBox):
                self._polish_message_box(watched)
            elif isinstance(watched, QInputDialog):
                self._polish_input_dialog(watched)
            if isinstance(watched, QDialog):
                self._lock_dialog_size(watched)
        return super().eventFilter(watched, event)

    @staticmethod
    def _lock_dialog_size(dialog: QDialog) -> None:
        """主画面以外のウィンドウは、表示時の設計サイズに固定する。"""
        dialog.setSizeGripEnabled(False)
        dialog.setFixedSize(dialog.size())

    @staticmethod
    def _polish_button_box(box: QDialogButtonBox) -> None:
        labels = {
            QDialogButtonBox.StandardButton.Ok: '確認',
            QDialogButtonBox.StandardButton.Yes: 'はい',
            QDialogButtonBox.StandardButton.No: 'いいえ',
            QDialogButtonBox.StandardButton.Save: '保存',
            QDialogButtonBox.StandardButton.Cancel: 'キャンセル',
            QDialogButtonBox.StandardButton.Close: '閉じる',
            QDialogButtonBox.StandardButton.Discard: '保存しない',
        }
        visible = []
        for standard, text in labels.items():
            button = box.button(standard)
            if button is None:
                continue
            button.setText(text)
            visible.append(button)
        for standard in (
            QDialogButtonBox.StandardButton.Ok,
            QDialogButtonBox.StandardButton.Yes,
            QDialogButtonBox.StandardButton.Save,
        ):
            button = box.button(standard)
            if button is not None:
                button.setProperty('primary', True)
                button.style().unpolish(button)
                button.style().polish(button)
                break
        if visible:
            width = max(88, *(button.sizeHint().width() for button in visible))
            for button in visible:
                button.setFixedWidth(width)

    def _polish_message_box(self, box: QMessageBox) -> None:
        box.setProperty('compactDialog', True)
        layout = box.layout()
        if layout is not None:
            # 標準 MessageBox の大きな余白を抑え、小型フォームと同じ密度にする。
            layout.setContentsMargins(22, 18, 22, 18)
            layout.setHorizontalSpacing(10)
            layout.setVerticalSpacing(14)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        icon = box.findChild(QLabel, 'qt_msgboxex_icon_label')
        if icon is not None and box.icon() == QMessageBox.Icon.Question:
            # 確認文とボタンだけで意図が明確なため、大きな「?」アイコンは表示しない。
            icon.hide()
        elif icon is not None and hasattr(icon, 'pixmap'):
            pixmap = icon.pixmap()
            if pixmap is not None and not pixmap.isNull():
                icon.setPixmap(pixmap.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        text_label = box.findChild(QLabel, 'qt_msgbox_label')
        if text_label is not None:
            text_label.setMinimumWidth(270 if box.icon() == QMessageBox.Icon.Question else 220)
        button_box = box.findChild(QDialogButtonBox)
        if button_box is not None:
            self._polish_button_box(button_box)
            self._polish_confirmation_buttons(box, button_box)
        box.adjustSize()

    @staticmethod
    def _polish_confirmation_buttons(box: QMessageBox, button_box: QDialogButtonBox) -> None:
        """削除確認では危険操作と取消を色と文言の両方で区別する。"""
        yes = button_box.button(QDialogButtonBox.StandardButton.Yes)
        no = button_box.button(QDialogButtonBox.StandardButton.No)
        if yes is None or no is None:
            return
        title = box.windowTitle()
        if '削除' in title:
            yes.setText('削除')
            no.setText('キャンセル')
        elif '删除' in title:
            yes.setText('删除')
            no.setText('取消')
        # 削除は通常の主操作ではないため、青色を外して危険色へ統一する。
        yes.setProperty('primary', False)
        yes.setProperty('danger', True)
        for button in (yes, no):
            button.style().unpolish(button)
            button.style().polish(button)
        width = max(96, yes.sizeHint().width(), no.sizeHint().width())
        yes.setFixedWidth(width)
        no.setFixedWidth(width)

    def _polish_input_dialog(self, dialog: QInputDialog) -> None:
        """単一入力ダイアログを他の固定サイズフォームと同じ密度に整える。"""
        dialog.setProperty('compactDialog', True)
        layout = dialog.layout()
        if layout is not None:
            layout.setContentsMargins(20, 18, 20, 16)
            # QInputDialog は入力種別によって VBox/Grid のどちらにもなる。
            layout.setSpacing(12)
        for editor in dialog.findChildren(QWidget):
            if isinstance(editor, (QComboBox, QAbstractSpinBox)) or editor.metaObject().className() == 'QLineEdit':
                editor.setMinimumWidth(300)
        button_box = dialog.findChild(QDialogButtonBox)
        if button_box is not None:
            self._polish_button_box(button_box)
        dialog.adjustSize()
        dialog.resize(max(420, dialog.width()), dialog.sizeHint().height())


class QtFlowManagerApplication:
    """Owns the Qt event loop and shared application services."""

    def __init__(self) -> None:
        frozen = getattr(sys, 'frozen', False)
        self.project_dir = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parents[1]
        (self.project_dir / 'data').mkdir(parents=True, exist_ok=True)
        (self.project_dir / 'log').mkdir(parents=True, exist_ok=True)
        self.db = Database(self.project_dir / 'data' / 'flows.db')
        set_language(self.db.get_language())
        # Windows のタスクバーで Python ではなく本アプリとして扱わせる。
        if sys.platform == 'win32':
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('WebFlowManager.Desktop')
            except (AttributeError, OSError):
                pass
        self.qt = QApplication.instance() or QApplication(sys.argv)
        self.qt.setApplicationName('WebFlowManager')
        # アプリケーションアイコンを先に設定し、全ダイアログへ継承させる。
        icon_path = self.project_dir / 'assets' / 'app-icon.png'
        if icon_path.is_file():
            self.qt.setWindowIcon(QIcon(str(icon_path)))
        family, size = self.db.get_ui_font()
        self.qt.setFont(QFont(family, size))
        theme_path = Path(__file__).with_name('theme.qss')
        arrow_path = Path(__file__).with_name('icons') / 'chevron-down.svg'
        up_arrow_path = Path(__file__).with_name('icons') / 'chevron-up.svg'
        spin_arrow_path = Path(__file__).with_name('icons') / 'spin-chevron-down.svg'
        spin_up_arrow_path = Path(__file__).with_name('icons') / 'spin-chevron-up.svg'
        check_path = Path(__file__).with_name('icons') / 'check.svg'
        theme = theme_path.read_text(encoding='utf-8')
        theme = (
            theme.replace('@CHEVRON_DOWN@', arrow_path.as_posix())
            .replace('@CHEVRON_UP@', up_arrow_path.as_posix())
            .replace('@SPIN_CHEVRON_DOWN@', spin_arrow_path.as_posix())
            .replace('@SPIN_CHEVRON_UP@', spin_up_arrow_path.as_posix())
            .replace('@CHECK_ICON@', check_path.as_posix())
        )
        self.qt.setStyleSheet(theme)
        self.combo_wheel_blocker = _ComboBoxWheelBlocker(self.qt)
        self.qt.installEventFilter(self.combo_wheel_blocker)
        self.window = MainWindow(self.project_dir, self.db)
        self.qt.aboutToQuit.connect(self.db.close)

    def run(self) -> int:
        self.window.show()
        return self.qt.exec()
