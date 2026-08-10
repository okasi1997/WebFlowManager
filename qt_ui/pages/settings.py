from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from core.database import Database
from i18n import tr
from ..ui_loader import load_ui_into, require, show_information, show_warning


class SettingsPage(QWidget):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        load_ui_into(self, 'settings.ui')

        root = require(self, QVBoxLayout, 'rootLayout')
        root.setStretch(2, 0)
        root.setSpacing(10)
        require(self, QLabel, 'titleLabel').setProperty('pageTitle', True)
        require(self, QLabel, 'subtitleLabel').setProperty('muted', True)

        # 二つの設定領域を同じ幅・高さに揃え、内容は上端にまとめる。
        cards = require(self, QHBoxLayout, 'cardsLayout')
        cards.setSpacing(12)
        cards.setAlignment(Qt.AlignmentFlag.AlignTop)
        cards.setStretch(0, 1)
        cards.setStretch(1, 1)
        for name in ('displayCard', 'runtimeCard'):
            card = require(self, QFrame, name)
            card.setProperty('card', True)
            card.setFixedHeight(330)
        for name in ('displayTitle', 'runtimeTitle'):
            require(self, QLabel, name).setProperty('cardTitle', True)
        for name in ('displayLayout', 'runtimeLayout'):
            layout = require(self, QVBoxLayout, name)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(12)
        for name in ('displayForm', 'runtimeForm'):
            form = require(self, QFormLayout, name)
            form.setContentsMargins(0, 0, 0, 0)
            form.setHorizontalSpacing(16)
            form.setVerticalSpacing(10)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.font_family = require(self, QComboBox, 'fontFamilyCombo')
        self.font_family.addItems(QFontDatabase.families())
        self.font_size = require(self, QComboBox, 'fontSizeCombo')
        self.font_size.addItems([str(value) for value in (8, 9, 10, 11, 12, 14, 16, 18)])
        self.language = require(self, QComboBox, 'languageCombo')
        self.language.addItem('日本語', 'ja')
        self.language.addItem('中文', 'zh')
        family, size = self.db.get_ui_font()
        self.font_family.setCurrentText(family)
        self.font_size.setCurrentText(str(size))
        self.language.setCurrentIndex(max(0, self.language.findData(self.db.get_language())))

        display_save = require(self, QPushButton, 'saveDisplayButton')
        require(self, QVBoxLayout, 'displayLayout').removeWidget(display_save)
        display_save.setObjectName('saveSettingsButton')
        display_save.setText('設定を保存')
        display_save.setProperty('primary', True)
        display_save.setFixedWidth(148)
        root.addWidget(display_save, 0, Qt.AlignmentFlag.AlignRight)
        root.addStretch(1)
        display_save.clicked.connect(self.save_all)

        self.start_url = require(self, QLineEdit, 'startUrlEdit')
        self.start_url.setText(self.db.get_start_url() or 'https://github.com/?locale=ja')
        self.start_url.setClearButtonEnabled(True)
        self.timeout = require(self, QSpinBox, 'timeoutSpin')
        require(self, QLabel, 'timeoutLabel').setText('既定タイムアウト')
        self.timeout.setSuffix(' ms')
        self.timeout.setSingleStep(1000)
        self.timeout.setValue(self.db.get_default_timeout_ms())
        self.browser_visible = require(self, QCheckBox, 'browserVisibleCheck')
        self.browser_visible.setChecked(self.db.get_browser_visible())
        self.session_limit = require(self, QSpinBox, 'sessionSpin')
        require(self, QLabel, 'sessionLabel').setText('同時セッション数')
        self.session_limit.setValue(self.db.get_pcl_session_limit())

        runtime_save = require(self, QPushButton, 'saveRuntimeButton')
        require(self, QVBoxLayout, 'runtimeLayout').removeWidget(runtime_save)
        runtime_save.setParent(None)
        runtime_save.deleteLater()

    def save_all(self) -> None:
        family, size = self.font_family.currentText(), int(self.font_size.currentText())
        language = str(self.language.currentData())
        start_url = self.start_url.text().strip()
        timeout = self.timeout.value()
        session_limit = self.session_limit.value()

        # 全項目を先に検証し、途中まで保存された状態を作らない。
        if not family.strip() or not 8 <= size <= 18:
            show_warning(self, tr('msg.0048'), 'フォント設定を確認してください。')
            return
        if language not in {'ja', 'zh'}:
            show_warning(self, tr('msg.0048'), '言語設定を確認してください。')
            return
        if not start_url.startswith(('http://', 'https://')):
            show_warning(self, tr('msg.0048'), '開始 URL は http:// または https:// から入力してください。')
            return
        if not 1 <= timeout <= 3600000 or not 1 <= session_limit <= 20:
            show_warning(self, tr('msg.0048'), '実行設定の数値を確認してください。')
            return

        self.db.set_ui_font(family, size)
        self.db.set_language(language)
        self.db.set_start_url(start_url)
        self.db.set_default_timeout_ms(timeout)
        self.db.set_browser_visible(self.browser_visible.isChecked())
        self.db.set_pcl_session_limit(session_limit)
        QApplication.instance().setFont(QFont(family, size))
        show_information(self, tr('msg.0048'), '設定を保存しました。言語の変更は再起動後に反映されます。')
