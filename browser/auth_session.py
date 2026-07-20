"""手動認証用ブラウザーと Playwright storage state の保存を管理する。"""
from __future__ import annotations

import queue
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
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
        browser = context = page = chrome_process = None

        def close() -> None:
            nonlocal browser, context, page, chrome_process
            try:
                if browser is not None:
                    browser.close()
            finally:
                if chrome_process is not None and chrome_process.poll() is None:
                    chrome_process.terminate()
                    try:
                        chrome_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        chrome_process.kill()
                browser = context = page = None
                chrome_process = None

        def chrome_executable() -> str:
            candidates = [
                shutil.which('chrome'), shutil.which('chrome.exe'),
                r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            ]
            for candidate in candidates:
                if candidate and Path(candidate).is_file():
                    return str(candidate)
            raise RuntimeError('msg.0482')

        def available_port() -> int:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                return int(listener.getsockname()[1])

        def open_browser(state_path: Path | None, url: str) -> None:
            nonlocal browser, context, page, chrome_process
            close()
            profile_name = state_path.stem if state_path is not None else 'none'
            user_data_dir = state_path.parent / 'chrome_profiles' / profile_name if state_path is not None else Path.cwd() / 'data' / 'chrome_profiles' / profile_name
            user_data_dir.mkdir(parents=True, exist_ok=True)
            port = available_port()
            chrome_process = subprocess.Popen([
                chrome_executable(), f'--remote-debugging-port={port}',
                f'--user-data-dir={user_data_dir}', '--start-maximized', url,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            endpoint = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(f'{endpoint}/json/version', timeout=1):
                        break
                except Exception:
                    if chrome_process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('msg.0483')
                    time.sleep(0.2)
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            pages = context.pages
            page = pages[-1] if pages else context.new_page()
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
