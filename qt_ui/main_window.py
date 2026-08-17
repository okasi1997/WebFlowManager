from __future__ import annotations

from pathlib import Path
from collections.abc import Callable, Iterator, Mapping

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QStackedWidget, QWidget

from core.database import Database
from browser.profile_runtime import clear_profile, persistent_profile_dir, profile_has_state
from i18n import tr
from .ui_loader import confirm_action, load_ui_into, require, set_button_icon, show_error


class LazyPageRegistry(Mapping[str, QWidget]):
    """必要になった画面だけを生成し、従来の pages[key] API を維持する。"""

    def __init__(
            self, factories: dict[str, Callable[[], QWidget]],
            on_created: Callable[[str, QWidget], None]) -> None:
        self._factories = factories
        self._on_created = on_created
        self._instances: dict[str, QWidget] = {}

    def __getitem__(self, key: str) -> QWidget:
        if key not in self._factories:
            raise KeyError(key)
        page = self._instances.get(key)
        if page is None:
            page = self._factories[key]()
            self._instances[key] = page
            self._on_created(key, page)
        return page

    def __iter__(self) -> Iterator[str]:
        return iter(self._factories)

    def __len__(self) -> int:
        return len(self._factories)

    def existing(self, key: str) -> QWidget | None:
        """未生成画面を作らずに、生成済み画面だけを取得する。"""
        return self._instances.get(key)

    def existing_values(self) -> tuple[QWidget, ...]:
        return tuple(self._instances.values())


class MainWindow(QMainWindow):
    DEFAULT_SIZE = QSize(1240, 780)
    MINIMUM_SIZE = QSize(1240, 640)

    def __init__(self, project_dir: Path, db: Database) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.db = db
        self._force_close = False
        self.setWindowTitle(tr('app.title'))
        # 実行管理で10行を確認でき、フロー設計の操作列まで表示できる初期サイズにする。
        self.resize(self.DEFAULT_SIZE)
        self.setMinimumSize(self.MINIMUM_SIZE)
        # QApplication に設定済みの共通アイコンを使用する。
        self.setWindowIcon(QApplication.windowIcon())

        root = load_ui_into(QWidget(), 'main_window.ui')
        self.setCentralWidget(root)
        self.stack = require(root, QStackedWidget, 'contentStack')
        self.pages = LazyPageRegistry({
            'auth': self._create_auth_page,
            'design': self._create_design_page,
            'schema': self._create_schema_page,
            'data': self._create_data_page,
            'execution': self._create_execution_page,
            'settings': self._create_settings_page,
        }, self._page_created)

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
        self.show_page('execution')

    def _page_created(self, key: str, page: QWidget) -> None:
        """遅延生成した画面をスタックへ登録し、画面固有の通知を接続する。"""
        self.stack.addWidget(page)
        if key == 'settings':
            page.settings_applied.connect(self._apply_saved_settings)
        # 遅延生成した画面のカード最小幅を、次のレイアウト計算後にウィンドウへ反映する。
        QTimer.singleShot(0, self._sync_content_minimum_width)

    def _sync_content_minimum_width(self) -> None:
        """全カードの最小幅が収まる実幅だけを、主ウィンドウの最小幅に設定する。"""
        root = self.centralWidget()
        if root is None:
            return
        root.layout().activate()
        required_width = max(self.MINIMUM_SIZE.width(), root.minimumSizeHint().width())
        self.setMinimumWidth(required_width)
        if self.width() < required_width:
            self.resize(required_width, self.height())

    def _create_auth_page(self) -> QWidget:
        from .pages.auth import AuthPage
        return AuthPage(self.project_dir, self.db)

    def _create_design_page(self) -> QWidget:
        from .pages.flow_design import FlowDesignPage
        return FlowDesignPage(self.project_dir, self.db)

    def _create_schema_page(self) -> QWidget:
        from .pages.structured import SchemaPage
        return SchemaPage(self.db)

    def _create_data_page(self) -> QWidget:
        from .pages.data import DataPage
        return DataPage(self.db)

    def _create_execution_page(self) -> QWidget:
        from .pages.execution import ExecutionPage
        return ExecutionPage(self.project_dir, self.db)

    def _create_settings_page(self) -> QWidget:
        from .pages.settings import SettingsPage
        return SettingsPage(self.db)

    def _apply_saved_settings(self) -> None:
        """保存済み設定を、現在生成済みの入力部品へ即時反映する。"""
        auth = self.pages.existing('auth')
        if auth is not None:
            auth.url.setText(self.db.get_start_url())
        execution = self.pages.existing('execution')
        if execution is not None:
            execution.session_spin.setValue(self.db.get_pcl_session_limit())
        # フォント変更後の sizeHint を使い、分割画面の操作ボタンが欠けない幅へ更新する。
        QTimer.singleShot(0, self._sync_content_minimum_width)

    def show_page(self, key: str) -> None:
        page = self.pages.existing(key)
        created = page is None
        if page is None:
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
        # 生成直後は各画面のコンストラクターで読込済みのため、二重読込を避ける。
        if callable(reload_page) and not created:
            reload_page()

    def preload_page(self, key: str) -> None:
        """画面を切り替えずに、指定画面の初回生成だけを先に完了する。"""
        if self.pages.existing(key) is None:
            self.pages[key]

    def closeEvent(self, event) -> None:
        if not self._force_close:
            current = self.stack.currentWidget()
            confirm = getattr(current, 'confirm_pending_changes', None)
            if callable(confirm) and not confirm():
                event.ignore()
                return
            execution = self.pages.existing('execution')
            if execution is not None and execution.is_executing() and not confirm_action(
                self, '終了確認', '実行中です。処理を中断して終了しますか？', confirm_text='終了',
            ):
                event.ignore()
                return

            from .pages.auth import profile_path
            state_path = profile_path(self.project_dir, self.db.get_auth_profile())
            profile_dir = persistent_profile_dir(self.project_dir, state_path)
            remove_profile = profile_has_state(profile_dir) and confirm_action(
                self, 'ログイン状態',
                '保存されているログイン状態があります。終了時に削除しますか？',
                confirm_text='削除',
            )
        else:
            profile_dir, remove_profile = None, False

        for page in self.pages.existing_values():
            shutdown = getattr(page, 'shutdown', None)
            if callable(shutdown):
                shutdown()
        if remove_profile and profile_dir is not None:
            try:
                clear_profile(self.project_dir, profile_dir)
            except (OSError, ValueError) as error:
                show_error(self, 'ログイン状態', str(error))
        super().closeEvent(event)
