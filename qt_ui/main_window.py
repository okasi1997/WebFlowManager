from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget, QWidget

from core.database import Database
from i18n import tr
from .pages.flow_design import FlowDesignPage
from .pages.auth import AuthPage
from .pages.placeholder import PlaceholderPage
from .pages.settings import SettingsPage
from .pages.data import DataPage
from .pages.structured import SchemaPage
from .pages.execution import ExecutionPage
from .ui_loader import load_ui_into, require, set_button_icon


class MainWindow(QMainWindow):
    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.db = db
        self.setWindowTitle(tr('msg.0002'))
        self.resize(1180, 720)
        self.setMinimumSize(QSize(1180, 720))
        # QApplication に設定済みの共通アイコンを使用する。
        self.setWindowIcon(QApplication.windowIcon())

        root = load_ui_into(QWidget(), 'main_window.ui')
        require(root, QHBoxLayout, 'rootLayout').setStretch(1, 1)
        require(root, QLabel, 'brandLabel').setObjectName('brand')
        require(root, QLabel, 'brandSubtitle').setObjectName('brandSubtle')
        for name in ('environmentSection', 'designSection', 'dataSection', 'executionSection'):
            require(root, QLabel, name).setProperty('class', 'sidebarSection')
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
            button.setProperty('nav', True)
            # 旧画面と同様に、文字だけでなく機能を識別できるアイコンを表示する。
            set_button_icon(button, nav_icons[key], 17)
            button.clicked.connect(lambda _checked=False, page=key: self.show_page(page))
        self.show_page('design')

    def show_page(self, key: str) -> None:
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        for name, button in self.nav_buttons.items():
            button.setProperty('navSelected', name == key)
            button.style().unpolish(button)
            button.style().polish(button)
        reload_page = getattr(page, 'reload', None)
        if callable(reload_page):
            reload_page()

    def closeEvent(self, event) -> None:
        for page in self.pages.values():
            shutdown = getattr(page, 'shutdown', None)
            if callable(shutdown):
                shutdown()
        super().closeEvent(event)
