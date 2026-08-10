from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import QFile, QIODevice, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel,
    QLayout, QPushButton, QStyle, QVBoxLayout, QWidget,
)


T = TypeVar('T', bound=QWidget)
UI_DIR = Path(__file__).with_name('forms')
ICON_DIR = Path(__file__).with_name('icons')


class ConfirmationDialog(QDialog):
    """質問・確認操作の見た目とキーボード動作を統一するダイアログ。"""

    def __init__(
        self, parent: QWidget, title: str, message: str, *,
        confirm_text: str = '確認', cancel_text: str | None = 'キャンセル',
        alternate_text: str | None = None,
        danger: bool = False,
        icon: QStyle.StandardPixmap = QStyle.StandardPixmap.SP_MessageBoxQuestion,
    ) -> None:
        super().__init__(parent)
        load_ui_into(self, 'confirmation.ui')
        self.setWindowTitle(title)
        self.setModal(True)
        icon_label = require(self, QLabel, 'confirmIcon')
        message_icon = QApplication.style().standardIcon(icon)
        icon_label.setPixmap(message_icon.pixmap(32, 32))
        message_label = require(self, QLabel, 'confirmMessage')
        message_label.setText(message)
        message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.confirm_button = require(self, QPushButton, 'confirmButton')
        self.confirm_button.setText(confirm_text)
        self.confirm_button.setProperty('danger' if danger else 'primary', True)
        self.confirm_button.setAutoDefault(cancel_text is None)
        self.choice = 'confirm'
        self.confirm_button.clicked.connect(lambda: self._finish('confirm'))
        self.alternate_button = require(self, QPushButton, 'alternateButton')
        if alternate_text is None:
            self.alternate_button.hide()
        else:
            self.alternate_button.setText(alternate_text)
            self.alternate_button.clicked.connect(lambda: self._finish('alternate'))
        cancel_button = require(self, QPushButton, 'cancelButton')
        self.cancel_button: QPushButton | None = cancel_button
        if cancel_text is not None:
            cancel_button.setText(cancel_text)
            # 誤操作を避けるため、確認画面の Enter キーはキャンセルを既定にする。
            cancel_button.setDefault(True)
            cancel_button.clicked.connect(self.reject)
        else:
            cancel_button.hide()
            self.cancel_button = None
            self.confirm_button.setDefault(True)
        (self.cancel_button or self.confirm_button).setFocus()

    def _finish(self, choice: str) -> None:
        """押された操作を保持して呼び出し元へ返す。"""
        self.choice = choice
        self.accept()


class DeletionConfirmDialog(ConfirmationDialog):
    """削除操作向けに危険色と文言を設定した共通確認ダイアログ。"""

    def __init__(self, parent: QWidget, message: str) -> None:
        super().__init__(
            parent, '削除', message, confirm_text='削除', danger=True,
        )
        # 既存呼び出し側との互換性を保つ別名。
        self.delete_button = self.confirm_button


def confirm_action(
    parent: QWidget, title: str, message: str, *, confirm_text: str = '確認',
) -> bool:
    """削除以外の質問・確認を共通デザインで表示する。"""
    return ConfirmationDialog(
        parent, title, message, confirm_text=confirm_text,
    ).exec() == QDialog.DialogCode.Accepted


def confirm_deletion(parent: QWidget, message: str) -> bool:
    """共通の削除確認を表示し、削除が選択された場合だけ True を返す。"""
    return DeletionConfirmDialog(parent, message).exec() == QDialog.DialogCode.Accepted


def confirm_pending_changes(parent: QWidget, message: str) -> str:
    """未保存内容について「保存・破棄・キャンセル」の選択結果を返す。"""
    dialog = ConfirmationDialog(
        parent, '未保存の変更', message,
        confirm_text='保存', alternate_text='保存しない', cancel_text='キャンセル',
        icon=QStyle.StandardPixmap.SP_MessageBoxWarning,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 'cancel'
    return 'discard' if dialog.choice == 'alternate' else 'save'


def _show_message(
    parent: QWidget, title: str, message: str, icon: QStyle.StandardPixmap,
) -> None:
    """単一ボタンの通知も確認画面と同じ共通レイアウトで表示する。"""
    ConfirmationDialog(
        parent, title, message, cancel_text=None, icon=icon,
    ).exec()


def show_information(parent: QWidget, title: str, message: str) -> None:
    _show_message(parent, title, message, QStyle.StandardPixmap.SP_MessageBoxInformation)


def show_warning(parent: QWidget, title: str, message: str) -> None:
    _show_message(parent, title, message, QStyle.StandardPixmap.SP_MessageBoxWarning)


def show_error(parent: QWidget, title: str, message: str) -> None:
    _show_message(parent, title, message, QStyle.StandardPixmap.SP_MessageBoxCritical)


def load_ui_into(target: T, filename: str) -> T:
    """Load a Designer form and move its root layout/children onto target."""
    source = QFile(str(UI_DIR / filename))
    if not source.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError(f'Cannot open Qt Designer form: {source.fileName()}')
    try:
        form = QUiLoader().load(source)
    finally:
        source.close()
    if form is None:
        raise RuntimeError(f'Cannot load Qt Designer form: {filename}')
    if not isinstance(target, QDialog):
        # ページ切替時に見出し位置が揺れないよう、主画面の余白を共通化する。
        page_layout = form.findChild(QLayout, 'rootLayout')
        if page_layout is not None:
            page_layout.setContentsMargins(28, 22, 28, 22)
    target.setObjectName(form.objectName())
    target.resize(form.size())
    if isinstance(target, QDialog):
        # ダイアログのサイズ制約も .ui を正とし、各画面での重複指定を不要にする。
        target.setMinimumSize(form.minimumSize())
        target.setMaximumSize(form.maximumSize())
    form.setWindowFlags(Qt.WindowType.Widget)
    wrapper = QVBoxLayout(target)
    wrapper.setContentsMargins(0, 0, 0, 0)
    wrapper.setSpacing(0)
    wrapper.addWidget(form)
    return target


def require(target: QWidget, widget_type: type[T], name: str) -> T:
    widget = target.findChild(widget_type, name)
    if widget is None:
        raise RuntimeError(f'Missing widget {name!r} in {target.objectName()!r}')
    return widget


def set_button_icon(button, icon_name: str, size: int = 16) -> None:
    """共通アイコンをボタンへ設定し、画面ごとの重複設定を避ける。"""
    icon_path = ICON_DIR / f'{icon_name}.svg'
    if not icon_path.exists():
        raise RuntimeError(f'Cannot find Qt icon: {icon_path}')
    button.setIcon(QIcon(str(icon_path)))
    button.setIconSize(QSize(size, size))


def localize_dialog_buttons(box: QDialogButtonBox) -> None:
    labels = {
        QDialogButtonBox.StandardButton.Save: '保存',
        QDialogButtonBox.StandardButton.Ok: '決定',
        QDialogButtonBox.StandardButton.Cancel: 'キャンセル',
    }
    for standard_button, text in labels.items():
        button = box.button(standard_button)
        if button is not None:
            button.setText(text)
            if standard_button in {QDialogButtonBox.StandardButton.Save, QDialogButtonBox.StandardButton.Ok}:
                button.setProperty('primary', True)
                button.style().unpolish(button)
                button.style().polish(button)
    visible_buttons = [button for button in box.buttons() if button.isVisibleTo(box)]
    if visible_buttons:
        common_width = max(button.sizeHint().width() for button in visible_buttons)
        for button in visible_buttons:
            button.setMinimumWidth(common_width)
