from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QLineEdit, QPushButton, QSpinBox, QWidget,
)

from core.database import Database
from i18n import tr
from ..ui_loader import (
    apply_application_font,
    confirm_pending_changes as ask_pending_changes,
    load_ui_into, require, show_information, show_warning,
)


class SettingsPage(QWidget):
    settings_applied = Signal()

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        load_ui_into(self, 'settings.ui')

        self.font_family = require(self, QComboBox, 'fontFamilyCombo')
        self.font_family.addItems(QFontDatabase.families())
        self.font_size = require(self, QComboBox, 'fontSizeCombo')
        self.font_size.addItems([str(value) for value in (8, 9, 10, 11, 12, 14, 16, 18)])
        self.language = require(self, QComboBox, 'languageCombo')
        self.language.addItem('日本語', 'ja')
        self.language.addItem('中文', 'zh')
        family, size = self.db.get_ui_font()
        family_index = self.font_family.findText(family)
        if family_index < 0:
            self.font_family.addItem(family)
            family_index = self.font_family.findText(family)
        self.font_family.setCurrentIndex(family_index if family_index >= 0 else 0)
        self.font_size.setCurrentText(str(size))
        self.language.setCurrentIndex(max(0, self.language.findData(self.db.get_language())))

        display_save = require(self, QPushButton, 'saveSettingsButton')
        display_save.clicked.connect(self.save_all)

        self.start_url = require(self, QLineEdit, 'startUrlEdit')
        self.start_url.setText(self.db.get_start_url() or 'https://github.com/?locale=ja')
        self.timeout = require(self, QSpinBox, 'timeoutSpin')
        self.timeout.setValue(self.db.get_default_timeout_ms())
        self.browser_visible = require(self, QCheckBox, 'browserVisibleCheck')
        self.browser_visible.setChecked(self.db.get_browser_visible())
        self.session_limit = require(self, QSpinBox, 'sessionSpin')
        self.session_limit.setValue(self.db.get_pcl_session_limit())
        self._saved_values = self._current_values()

    def _current_values(self) -> tuple[object, ...]:
        return (
            self.font_family.currentText(), self.font_size.currentText(),
            self.language.currentData(), self.start_url.text(), self.timeout.value(),
            self.session_limit.value(), self.browser_visible.isChecked(),
        )

    def has_pending_changes(self) -> bool:
        return self._current_values() != self._saved_values

    def confirm_pending_changes(self) -> bool:
        if not self.has_pending_changes():
            return True
        choice = ask_pending_changes(self, '設定に未保存の変更があります。保存しますか？')
        if choice == 'save':
            return self.save_all(show_message=False)
        if choice == 'discard':
            self._restore_saved_values()
            return True
        return False

    def _restore_saved_values(self) -> None:
        family, size, language, url, timeout, session, visible = self._saved_values
        self.font_family.setCurrentText(str(family))
        self.font_size.setCurrentText(str(size))
        self.language.setCurrentIndex(max(0, self.language.findData(language)))
        self.start_url.setText(str(url))
        self.timeout.setValue(int(timeout))
        self.session_limit.setValue(int(session))
        self.browser_visible.setChecked(bool(visible))

    def save_all(self, _checked: bool = False, *, show_message: bool = True) -> bool:
        family, size = self.font_family.currentText(), int(self.font_size.currentText())
        language = str(self.language.currentData())
        start_url = self.start_url.text().strip()
        timeout = self.timeout.value()
        session_limit = self.session_limit.value()

        # 全項目を先に検証し、途中まで保存された状態を作らない。
        if not family.strip() or not 8 <= size <= 18:
            show_warning(self, tr('common.notice'), 'フォント設定を確認してください。')
            return False
        if language not in {'ja', 'zh'}:
            show_warning(self, tr('common.notice'), '言語設定を確認してください。')
            return False
        if not start_url.startswith(('http://', 'https://')):
            show_warning(self, tr('common.notice'), '開始 URL は http:// または https:// から入力してください。')
            return False
        if not 1 <= timeout <= 3600000 or not 1 <= session_limit <= 20:
            show_warning(self, tr('common.notice'), '実行設定の数値を確認してください。')
            return False

        self.db.set_ui_font(family, size)
        self.db.set_language(language)
        self.db.set_start_url(start_url)
        self.db.set_default_timeout_ms(timeout)
        self.db.set_browser_visible(self.browser_visible.isChecked())
        self.db.set_pcl_session_limit(session_limit)
        apply_application_font(family, size)
        self._saved_values = self._current_values()
        # 言語以外は再起動を待たず、既に開いている画面にも反映する。
        self.settings_applied.emit()
        if show_message:
            show_information(self, tr('common.notice'), '設定を保存しました。言語の変更は再起動後に反映されます。')
        return True
