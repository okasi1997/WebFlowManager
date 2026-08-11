from __future__ import annotations

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHeaderView, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QWidget,
)

from browser.auth_session import AuthBrowserSession
from browser.profile_runtime import clear_profile, persistent_profile_dir
from core.database import Database
from i18n import tr
from ..table_view import configure_table_view
from ..table_view import capture_scroll_position, restore_scroll_position
from ..ui_loader import confirm_deletion, load_ui_into, require, show_information, show_warning

DEFAULT_PROFILE = 'default'
NO_PROFILE = 'none'
GENERATED_GROUP_STATE_SUFFIX = re.compile(r'_group_[A-Za-z0-9_-]+$')


def profile_path(project_dir: Path, profile: str) -> Path | None:
    if profile == NO_PROFILE:
        return None
    if profile == DEFAULT_PROFILE:
        return project_dir / 'data' / 'browser_state.json'
    return project_dir / 'data' / 'browser_states' / f'{profile}.json'


class AuthPage(QWidget):
    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.db = db
        self.session = AuthBrowserSession(project_dir, lambda _message: None)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qt-auth')
        load_ui_into(self, 'auth.ui')
        body_layout = require(self, QHBoxLayout, 'bodyLayout')
        self.profiles = require(self, QTreeWidget, 'profileTree')
        configure_table_view(self.profiles)
        # 小さい画面では名前列だけを縮め、状態列が常に見えるようにする。
        profile_header = self.profiles.header()
        profile_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        profile_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.profiles.setColumnWidth(1, 82)
        self.profiles.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.profiles.currentItemChanged.connect(
            lambda current, _previous: self.select_profile(current.text(0)) if current else None
        )
        create = require(self, QPushButton, 'newProfileButton')
        self.create_profile_button = create
        delete = require(self, QPushButton, 'deleteProfileButton')
        create.clicked.connect(self.new_profile)
        delete.clicked.connect(self.delete_profile)
        self.selected = require(self, QLabel, 'selectedLabel')
        self.path = require(self, QLabel, 'pathLabel')
        self.url = require(self, QLineEdit, 'urlEdit')
        self.url.setText(self.db.get_start_url() or 'https://github.com/?locale=ja')
        open_button = require(self, QPushButton, 'openBrowserButton')
        save_button = require(self, QPushButton, 'saveStateButton')
        self.save_state_button = save_button
        close_button = require(self, QPushButton, 'closeBrowserButton')
        open_button.clicked.connect(self.open_browser)
        save_button.clicked.connect(self.save_state)
        close_button.clicked.connect(self.close_browser)
        self.status = require(self, QLabel, 'statusLabel')
        self.reload()
        QTimer.singleShot(0, self._sync_save_button_width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_save_button_width)

    def _sync_save_button_width(self) -> None:
        width = self.create_profile_button.width()
        if width > 0 and self.save_state_button.width() != width:
            self.save_state_button.setFixedWidth(width)

    def _profile_names(self) -> list[str]:
        folder = self.project_dir / 'data' / 'browser_states'
        custom = sorted((p.stem for p in folder.glob('*.json') if not GENERATED_GROUP_STATE_SUFFIX.search(p.stem)), key=str.casefold) if folder.exists() else []
        return [DEFAULT_PROFILE, NO_PROFILE, *custom]

    def reload(self) -> None:
        scroll = capture_scroll_position(self.profiles)
        selected = self.db.get_auth_profile()
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for name in self._profile_names():
            self.profiles.addTopLevelItem(QTreeWidgetItem([name, tr('使用中') if name == selected else '']))
        matches = self.profiles.findItems(selected, Qt.MatchFlag.MatchExactly, 0)
        self.profiles.setCurrentItem(matches[0] if matches else self.profiles.topLevelItem(0))
        self.profiles.blockSignals(False)
        self.select_profile(self.profiles.currentItem().text(0))
        restore_scroll_position(self.profiles, scroll)

    def select_profile(self, name: str) -> None:
        if not name:
            return
        self.db.set_auth_profile(name)
        for index in range(self.profiles.topLevelItemCount()):
            item = self.profiles.topLevelItem(index)
            item.setText(1, tr('使用中') if item.text(0) == name else '')
        self.selected.setText(name)
        directory = persistent_profile_dir(self.project_dir, profile_path(self.project_dir, name))
        self.path.setText(tr('login.no_state_description') if directory is None else str(directory))

    def new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, tr('common.new'), tr('login.state_name_prompt'))
        name = name.strip()
        if not ok or not name:
            return
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in {DEFAULT_PROFILE, NO_PROFILE}:
            show_warning(self, tr('error.input_title'), tr('login.state_name_invalid'))
            return
        path = profile_path(self.project_dir, name)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        self.db.set_auth_profile(name)
        self.reload()

    def delete_profile(self) -> None:
        name = self.profiles.currentItem().text(0)
        if name in {DEFAULT_PROFILE, NO_PROFILE}:
            show_information(self, tr('common.notice'), tr('login.protected_state_delete_denied'))
            return
        if not confirm_deletion(self, f'{name} を削除しますか？'):
            return
        path = profile_path(self.project_dir, name)
        if path is not None and path.exists():
            path.unlink()
        directory = persistent_profile_dir(self.project_dir, path)
        if directory is not None:
            clear_profile(self.project_dir, directory)
        self.db.set_auth_profile(DEFAULT_PROFILE)
        self.reload()

    def _background(self, operation, success: str) -> None:
        self.status.setText(tr('login.processing'))
        future: Future = self.pool.submit(operation)
        timer = QTimer(self)
        timer.setInterval(80)
        def poll() -> None:
            if not future.done():
                return
            timer.stop()
            try:
                result = future.result()
                self.status.setText(tr(success).format(result=result))
            except Exception as error:
                self.status.setText(tr(str(error)))
        timer.timeout.connect(poll)
        timer.start()

    def open_browser(self) -> None:
        path = profile_path(self.project_dir, self.profiles.currentItem().text(0))
        self._background(lambda: self.session.open(path, self.url.text()), tr('login.browser_opened'))

    def save_state(self) -> None:
        path = profile_path(self.project_dir, self.profiles.currentItem().text(0))
        if path is None:
            show_information(self, tr('common.notice'), tr('login.no_state_save_denied'))
            return
        self._background(lambda: self.session.save(path), '保存しました: {result}')

    def close_browser(self) -> None:
        self._background(self.session.close_browser, tr('login.browser_closed'))

    def shutdown(self) -> None:
        self.session.shutdown()
        self.pool.shutdown(wait=False, cancel_futures=True)
