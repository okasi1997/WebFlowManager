"""手動認証用ブラウザーと Playwright storage state の保存を管理する。"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable
from browser.page_runtime import active_page, close_browser_context, launch_persistent_chrome, restore_storage_state
from browser.profile_runtime import persistent_profile_dir


class AuthBrowserSession:
    def __init__(self, project_dir: Path, logger: Callable[[str], None]) -> None:
        self.project_dir = project_dir
        self.logger = logger
        self._tasks: queue.Queue[tuple[Callable[[], Any] | None, Future[Any]]] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, name='auth-browser', daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            while True:
                task, future = self._tasks.get()
                if task is None:
                    future.set_result(None)
                    return
                future.set_exception(RuntimeError('error.playwright_not_installed'))
        playwright = sync_playwright().start()
        context = page = None

        def close() -> None:
            nonlocal context, page
            try:
                close_browser_context(context)
            finally:
                context = page = None

        def open_browser(state_path: Path | None, url: str) -> None:
            nonlocal context, page
            close()
            user_data_dir = persistent_profile_dir(self.project_dir, state_path)
            if user_data_dir is None:
                raise RuntimeError('保存対象のログイン状態を選択してください。')
            try:
                context = launch_persistent_chrome(playwright, user_data_dir, visible=True)
                restore_storage_state(context, state_path)
                pages = context.pages
                page = pages[-1] if pages else context.new_page()
                page.goto(url, wait_until='domcontentloaded')
                page.bring_to_front()
            except Exception:
                close()
                raise

        def save(state_path: Path) -> str:
            nonlocal page
            if context is None or page is None:
                raise RuntimeError('login.browser_not_open')
            page = active_page(page)
            if page.is_closed():
                raise RuntimeError('login.browser_not_open')
            state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(state_path))
            return page.url

        def status() -> tuple[str, str, int]:
            nonlocal page
            if context is None or page is None:
                raise RuntimeError('login.browser_not_open')
            page = active_page(page)
            if page.is_closed():
                raise RuntimeError('login.browser_not_open')
            return page.url, page.title(), len(context.cookies())

        self._open_browser, self._save, self._status, self._close_browser = open_browser, save, status, close
        while True:
            try:
                task, future = self._tasks.get(timeout=0.1)
            except queue.Empty:
                # 同期 API の呼出しが途切れると、新規タブの Target が一時停止したまま
                # になるため、待機中も短い呼出しで Playwright のイベントを処理する。
                try:
                    if page is not None and not page.is_closed():
                        page.wait_for_timeout(50)
                except Exception:
                    pass
                continue
            if task is None:
                try:
                    close()
                    playwright.stop()
                    future.set_result(None)
                except Exception as error:
                    future.set_exception(error)
                return
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)

    def _submit(self, task: Callable[[], Any], timeout: float | None=None) -> Any:
        future: Future[Any] = Future()
        self._tasks.put((task, future))
        return future.result(timeout=timeout)

    def open(self, state_path: Path | None, url: str) -> None:
        self._submit(lambda: self._open_browser(state_path, url))

    def save(self, state_path: Path) -> str:
        return str(self._submit(lambda: self._save(state_path)))

    def status(self) -> tuple[str, str, int]:
        # ワーカースレッドの初期化完了後にメソッドを解決し、起動直後の競合を避ける。
        return self._submit(lambda: self._status())

    def close_browser(self) -> None:
        # アプリ起動直後でも未生成の属性を呼出し側スレッドで参照しない。
        try:
            self._submit(lambda: self._close_browser(), timeout=10)
        except FutureTimeoutError as error:
            raise RuntimeError('browser.close_timeout') from error

    def shutdown(self) -> None:
        future: Future[Any] = Future()
        self._tasks.put((None, future))
        try:
            future.result(timeout=5)
        except Exception:
            pass
