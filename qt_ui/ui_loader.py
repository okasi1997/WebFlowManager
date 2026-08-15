from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import QFile, QIODevice, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGroupBox, QLabel, QLayout, QLineEdit, QMenu, QPlainTextEdit,
    QPushButton, QStyle, QTabWidget, QTableWidget, QTextEdit, QTreeWidget,
    QVBoxLayout, QWidget,
)

from i18n import tr


T = TypeVar('T', bound=QWidget)
UI_DIR = Path(__file__).with_name('forms')
ICON_DIR = Path(__file__).with_name('icons')


def optically_align_form_labels(*forms: QFormLayout) -> None:
    """入力文字のベースラインに合わせ、フォームラベルを視覚上わずかに下げる。"""
    for form in forms:
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            label = item.widget() if item is not None else None
            if isinstance(label, QLabel):
                # 幾何学的な中央では文字が上寄りに見えるため、内容領域だけを 2px 補正する。
                label.setContentsMargins(0, 2, 0, 0)


def localize_widget_texts(root: QWidget) -> None:
    """Designer と Python で設定された固定表示文言をまとめて翻訳する。"""
    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        if widget.windowTitle():
            widget.setWindowTitle(tr(widget.windowTitle()))
        if widget.toolTip():
            widget.setToolTip(tr(widget.toolTip()))
        if isinstance(widget, (QLabel, QAbstractButton, QGroupBox)):
            widget.setText(tr(widget.text())) if not isinstance(widget, QGroupBox) else widget.setTitle(tr(widget.title()))
        if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)) and widget.placeholderText():
            widget.setPlaceholderText(tr(widget.placeholderText()))
        if isinstance(widget, QComboBox):
            for index in range(widget.count()):
                widget.setItemText(index, tr(widget.itemText(index)))
        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                widget.setTabText(index, tr(widget.tabText(index)))
        if isinstance(widget, QTableWidget):
            for column in range(widget.columnCount()):
                item = widget.horizontalHeaderItem(column)
                if item is not None:
                    item.setText(tr(item.text()))
        if isinstance(widget, QTreeWidget):
            header = widget.headerItem()
            for column in range(widget.columnCount()):
                header.setText(column, tr(header.text(column)))
        if isinstance(widget, QMenu):
            for action in widget.actions():
                action.setText(tr(action.text()))


def apply_application_font(family: str, size: int) -> None:
    """全体フォントを、既に生成済みの表・ヘッダーを含む全ウィジェットへ反映する。"""
    application = QApplication.instance()
    if application is None:
        return
    font = QFont(family, size)
    application.setFont(font)
    # QApplication の既定値変更だけでは、スタイル適用済みの viewport や header が
    # 古い解決済みフォントを保持する場合があるため、生成済み部品にも明示する。
    for widget in application.allWidgets():
        widget.setFont(font)
        widget.updateGeometry()
        widget.update()


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
        self.setWindowTitle(tr(title))
        self.setModal(True)
        icon_label = require(self, QLabel, 'confirmIcon')
        message_icon = QApplication.style().standardIcon(icon)
        icon_label.setPixmap(message_icon.pixmap(32, 32))
        translated_message = tr(message)
        message_label = require(self, QLabel, 'confirmMessage')
        message_label.setText(translated_message)
        message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        message_details = require(self, QPlainTextEdit, 'messageDetails')
        message_details.setPlainText(translated_message)
        # 長いエラーや複数の検証結果は、スクロール可能な詳細欄で欠落なく表示する。
        if self._requires_message_details(translated_message):
            message_label.hide()
            message_details.show()
            self._resize_for_message_details()
        self.confirm_button = require(self, QPushButton, 'confirmButton')
        self.confirm_button.setText(tr(confirm_text))
        self.confirm_button.setProperty('danger' if danger else 'primary', True)
        self.confirm_button.setAutoDefault(cancel_text is None)
        self.choice = 'confirm'
        self.confirm_button.clicked.connect(lambda: self._finish('confirm'))
        self.alternate_button = require(self, QPushButton, 'alternateButton')
        if alternate_text is None:
            self.alternate_button.hide()
        else:
            self.alternate_button.setText(tr(alternate_text))
            self.alternate_button.clicked.connect(lambda: self._finish('alternate'))
        cancel_button = require(self, QPushButton, 'cancelButton')
        self.cancel_button: QPushButton | None = cancel_button
        if cancel_text is not None:
            cancel_button.setText(tr(cancel_text))
            # 誤操作を避けるため、確認画面の Enter キーはキャンセルを既定にする。
            cancel_button.setDefault(True)
            cancel_button.clicked.connect(self.reject)
        else:
            cancel_button.hide()
            self.cancel_button = None
            self.confirm_button.setDefault(True)
        (self.cancel_button or self.confirm_button).setFocus()

    @staticmethod
    def _requires_message_details(message: str) -> bool:
        """固定サイズのラベルでは読みづらいメッセージかを共通判定する。"""
        return len(message) > 180 or message.count('\n') >= 3

    def _resize_for_message_details(self) -> None:
        """長文表示を画面内に収まる範囲で拡張する。"""
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = min(620, available.width() - 40) if available is not None else 620
        height = min(360, available.height() - 40) if available is not None else 360
        self.resize(max(380, width), max(240, height))

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


def confirm_import_overwrite(
        parent: QWidget, has_existing: bool, target_name: str,
) -> bool:
    """既存内容を置き換える場合だけ、共通デザインで読込確認を表示する。"""
    if not has_existing:
        return True
    return confirm_action(
        parent, tr('読込確認'),
        f'{tr("既存の")}{tr(target_name)}'
        f'{tr("は読み込んだ内容で置き換えられます。続行しますか？")}',
        confirm_text=tr('読み込む'),
    )


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


def show_file_exported(parent: QWidget, path: str | Path) -> None:
    """ファイル出力の完了通知を、各画面で共通表示する。"""
    show_information(parent, tr('common.file_exported_title'), f'{tr("出力しました：\n")}{path}')


def show_file_imported(parent: QWidget, path: str | Path) -> None:
    """ファイル読込の完了通知を、各画面で共通表示する。"""
    show_information(parent, tr('common.file_imported_title'), f'{tr("読み込みました：\n")}{path}')


def load_ui_into(target: T, filename: str) -> T:
    """Designer フォームを読み込み、ルートの配置と子部品を対象へ移す。"""
    source = QFile(str(UI_DIR / filename))
    if not source.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError(f'Cannot open Qt Designer form: {source.fileName()}')
    try:
        form = QUiLoader().load(source)
    finally:
        source.close()
    if form is None:
        raise RuntimeError(f'Cannot load Qt Designer form: {filename}')
    # Data 構造参照ボタンは Designer の識別プロパティから共通 SVG を設定する。
    for button in form.findChildren(QPushButton):
        if button.property('dataReferenceButton'):
            set_button_icon(button, 'data-reference', 18)
    # フォントサイズが変わっても、フォームの項目名を入力欄の中央へ揃える。
    # Designer の labelAlignment を持たない旧フォームにも同じ規則を適用する。
    for form_layout in form.findChildren(QFormLayout):
        form_layout.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        for row in range(form_layout.rowCount()):
            label_item = form_layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
            label = label_item.widget() if label_item is not None else None
            if isinstance(label, QLabel):
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                form_layout.setAlignment(label, Qt.AlignmentFlag.AlignVCenter)
                field_item = form_layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                field = field_item.widget() if field_item is not None else None
                if isinstance(field, QWidget):
                    # 小さいフォントでもラベル領域を入力欄と同じ高さにし、基準線寄せを防ぐ。
                    label.setMinimumHeight(max(label.minimumHeight(), field.sizeHint().height()))
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
        if isinstance(form, QDialog):
            # .ui の QDialog は外側の Python ダイアログへ埋め込まれる。
            # Esc で内側だけが reject されると空の外枠が残るため、外側にも伝播する。
            form.rejected.connect(target.reject)
    form.setWindowFlags(Qt.WindowType.Widget)
    wrapper = QVBoxLayout(target)
    wrapper.setContentsMargins(0, 0, 0, 0)
    wrapper.setSpacing(0)
    wrapper.addWidget(form)
    # 各画面のコンストラクターが追加する選択肢やメニューも、初回表示前にまとめて翻訳する。
    QTimer.singleShot(0, lambda target=target: localize_widget_texts(target))
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
    # パッケージ版でも無効ボタンの SVG が消えないよう、各表示モードを明示登録する。
    icon = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
        icon.addFile(str(icon_path), QSize(), mode, QIcon.State.Off)
    button.setIcon(icon)
    button.setIconSize(QSize(size, size))


def set_tree_toggle_icon(button: QPushButton, expand: bool) -> None:
    """ツリーの一括展開・折りたたみボタンを共通のアイコン表示へ更新する。"""
    button.setText('')
    set_button_icon(button, 'expand-all' if expand else 'collapse-all', 18)
    button.setToolTip(tr('すべて展開' if expand else 'すべて折りたたむ'))


def localize_dialog_buttons(box: QDialogButtonBox) -> None:
    labels = {
        QDialogButtonBox.StandardButton.Save: '保存',
        QDialogButtonBox.StandardButton.Ok: '決定',
        QDialogButtonBox.StandardButton.Cancel: 'キャンセル',
    }
    for standard_button, text in labels.items():
        button = box.button(standard_button)
        if button is not None:
            button.setText(tr(text))
            if standard_button in {QDialogButtonBox.StandardButton.Save, QDialogButtonBox.StandardButton.Ok}:
                button.setProperty('primary', True)
                button.style().unpolish(button)
                button.style().polish(button)
    visible_buttons = [button for button in box.buttons() if button.isVisibleTo(box)]
    if visible_buttons:
        common_width = max(button.sizeHint().width() for button in visible_buttons)
        for button in visible_buttons:
            button.setMinimumWidth(common_width)
