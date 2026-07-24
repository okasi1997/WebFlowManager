"""手動認証用ブラウザーと Playwright storage state の保存を管理する。"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable


class AuthBrowserSession:
    def __init__(self, logger: Callable[[str], None]) -> None:
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
                future.set_exception(RuntimeError('msg.0169'))
        playwright = sync_playwright().start()
        context = page = None

        def close() -> None:
            nonlocal context, page
            try:
                if context is not None:
                    context.close()
            finally:
                context = page = None

        def open_browser(state_path: Path | None, url: str) -> None:
            nonlocal context, page
            close()
            profile_name = state_path.stem if state_path is not None else 'none'
            user_data_dir = state_path.parent / 'chrome_profiles' / profile_name if state_path is not None else Path.cwd() / 'data' / 'chrome_profiles' / profile_name
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel='chrome',
                headless=False,
                args=['--start-maximized'],
                no_viewport=True,
            )
            pages = context.pages
            page = pages[-1] if pages else context.new_page()
            page.goto(url, wait_until='domcontentloaded')
            page.bring_to_front()

        def save(state_path: Path) -> str:
            if context is None or page is None or page.is_closed():
                raise RuntimeError('msg.0468')
            state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(state_path))
            return page.url

        def status() -> tuple[str, str, int]:
            if context is None or page is None or page.is_closed():
                raise RuntimeError('msg.0468')
            return page.url, page.title(), len(context.cookies())

        self._open_browser, self._save, self._status, self._close_browser = open_browser, save, status, close
        while True:
            task, future = self._tasks.get()
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

    def _submit(self, task: Callable[[], Any]) -> Any:
        future: Future[Any] = Future()
        self._tasks.put((task, future))
        return future.result()

    def open(self, state_path: Path | None, url: str) -> None:
        self._submit(lambda: self._open_browser(state_path, url))

    def save(self, state_path: Path) -> str:
        return str(self._submit(lambda: self._save(state_path)))

    def status(self) -> tuple[str, str, int]:
        return self._submit(self._status)

    def close_browser(self) -> None:
        self._submit(self._close_browser)

    def shutdown(self) -> None:
        future: Future[Any] = Future()
        self._tasks.put((None, future))
        try:
            future.result(timeout=5)
        except Exception:
            pass
