from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QStackedWidget, QWidget

from core.database import Database
from browser.profile_runtime import clear_profile, persistent_profile_dir, profile_has_state
from i18n import tr
from .pages.flow_design import FlowDesignPage
from .pages.auth import AuthPage, profile_path
from .pages.settings import SettingsPage
from .pages.data import DataPage
from .pages.structured import SchemaPage
from .pages.execution import ExecutionPage
from .ui_loader import confirm_action, load_ui_into, require, set_button_icon, show_error


class MainWindow(QMainWindow):
    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.db = db
        self._force_close = False
        self.setWindowTitle(tr('app.title'))
        self.resize(1180, 720)
        self.setMinimumSize(QSize(1180, 720))
        # QApplication に設定済みの共通アイコンを使用する。
        self.setWindowIcon(QApplication.windowIcon())

        root = load_ui_into(QWidget(), 'main_window.ui')
        self.setCentralWidget(root)
        self.stack = require(root, QStackedWidget, 'contentStack')
        self.pages = {
            'auth': AuthPage(project_dir, db),
            'design': FlowDesignPage(project_dir, db),
            'schema': SchemaPage(db),
            'data': DataPage(db),
            'execution': ExecutionPage(project_dir, db),
            'settings': SettingsPage(db),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.pages['settings'].settings_applied.connect(self._apply_saved_settings)

        self.nav_buttons = {
            key: require(root, QPushButton, f'{key}Nav')
            for key in ('auth', 'design', 'schema', 'data', 'execution', 'settings')
        }
        nav_icons = {
            'auth': 'nav-auth',
            'design': 'nav-flow',
            'schema': 'nav-schema',
            'data': 'nav-data',
            'execution': 'nav-execution',
            'settings': 'nav-settings',
        }
        for key, button in self.nav_buttons.items():
            # 旧画面と同様に、文字だけでなく機能を識別できるアイコンを表示する。
            set_button_icon(button, nav_icons[key], 17)
            button.clicked.connect(lambda _checked=False, page=key: self.show_page(page))
        self.show_page('design')

    def _apply_saved_settings(self) -> None:
        """保存済み設定を、現在生成済みの入力部品へ即時反映する。"""
        self.pages['auth'].url.setText(self.db.get_start_url())
        self.pages['execution'].session_spin.setValue(self.db.get_pcl_session_limit())

    def show_page(self, key: str) -> None:
        page = self.pages[key]
        current = self.stack.currentWidget()
        if current is not None and current is not page:
            confirm = getattr(current, 'confirm_pending_changes', None)
            if callable(confirm) and not confirm():
                return
        self.stack.setCurrentWidget(page)
        for name, button in self.nav_buttons.items():
            button.setProperty('navSelected', name == key)
            button.style().unpolish(button)
            button.style().polish(button)
        reload_page = getattr(page, 'reload', None)
        if callable(reload_page):
            reload_page()

    def closeEvent(self, event) -> None:
        if not self._force_close:
            current = self.stack.currentWidget()
            confirm = getattr(current, 'confirm_pending_changes', None)
            if callable(confirm) and not confirm():
                event.ignore()
                return
            execution = self.pages['execution']
            if execution.future is not None and not confirm_action(
                self, '終了確認', '実行中です。処理を中断して終了しますか？', confirm_text='終了',
            ):
                event.ignore()
                return

            state_path = profile_path(self.project_dir, self.db.get_auth_profile())
            profile_dir = persistent_profile_dir(self.project_dir, state_path)
            remove_profile = profile_has_state(profile_dir) and confirm_action(
                self, 'ログイン状態',
                '保存されているログイン状態があります。終了時に削除しますか？',
                confirm_text='削除',
            )
        else:
            profile_dir, remove_profile = None, False

        for page in self.pages.values():
            shutdown = getattr(page, 'shutdown', None)
            if callable(shutdown):
                shutdown()
        if remove_profile and profile_dir is not None:
            try:
                clear_profile(self.project_dir, profile_dir)
            except (OSError, ValueError) as error:
                show_error(self, 'ログイン状態', str(error))
        super().closeEvent(event)
