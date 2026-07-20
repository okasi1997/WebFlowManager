"""ブラウザー上で要素を選択し、一意な locator 候補を生成する。"""
from __future__ import annotations
import queue
import threading
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from i18n import tr
PICKER_SCRIPT = 'msg.0167'
TEST_READY_SCRIPT = 'msg.0168'

class ElementPicker:
    """F2 で選択モードへ入り、操作可能な要素だけを候補として返す。"""

    def __init__(self, project_dir: Path, start_url: str) -> None:
        self.project_dir = project_dir
        self.start_url = start_url

    def pick(self) -> dict[str, str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError('msg.0169') from error
        state_path = self.project_dir / 'data' / 'browser_state.json'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=False, args=['--start-maximized'])
            options: dict[str, Any] = {'no_viewport': True}
            if state_path.exists():
                options['storage_state'] = str(state_path)
            context = browser.new_context(**options)
            page = context.new_page()
            try:
                page.goto(self.start_url, wait_until='domcontentloaded')
                while True:
                    page.wait_for_timeout(500)
                    try:
                        installed = page.evaluate('() => window.__sfFlowPicked !== undefined')
                        if not installed:
                            page.evaluate(tr(PICKER_SCRIPT))
                        result = page.evaluate('() => window.__sfFlowPicked')
                        if result:
                            if result.get('cancelled'):
                                raise RuntimeError('msg.0170')
                            context.storage_state(path=str(state_path))
                            return self._choose_unique_locator(page, result)
                    except RuntimeError:
                        raise
                    except Exception:
                        if page.is_closed():
                            raise RuntimeError('msg.0171')
            finally:
                context.close()
                browser.close()

    def _choose_unique_locator(self, page: Any, info: dict[str, str]) -> dict[str, str]:
        # 人が理解しやすい locator から順に試し、XPath は予備として保持する。
        action = 'fill' if info.get('role') in ('textbox', 'combobox') else 'click'
        display = info.get('label') or info.get('name') or info.get('text') or info.get('tag')
        candidates: list[tuple[str, str]] = []
        if info.get('label'):
            candidates.append(('label', info['label']))
        if info.get('role') and info.get('name'):
            candidates.append(('role', f'{info['role']}|{info['name']}'))
        if info.get('placeholder'):
            candidates.append(('placeholder', info['placeholder']))
        if info.get('text'):
            candidates.append(('text', info['text']))
        if info.get('css'):
            candidates.append(('css', info['css']))
        if info.get('xpath'):
            candidates.append(('xpath', info['xpath']))
        for selector_type, selector in candidates:
            locator = self._locator(page, selector_type, selector)
            actionable = self._actionable_matches(locator)
            if len(actionable) == 1:
                return {'selector_type': selector_type, 'selector': selector, 'fallback_selector_type': 'xpath' if selector_type != 'xpath' and info.get('xpath') else 'none', 'fallback_selector': info.get('xpath', '') if selector_type != 'xpath' else '', 'display': display, 'suggested_action': action, 'match_count': '1'}
        raise RuntimeError('msg.0172')

    def test(self, selector_type: str, selector: str) -> int:
        from playwright.sync_api import sync_playwright
        state_path = self.project_dir / 'data' / 'browser_state.json'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=False, args=['--start-maximized'])
            options: dict[str, Any] = {'no_viewport': True}
            if state_path.exists():
                options['storage_state'] = str(state_path)
            context = browser.new_context(**options)
            page = context.new_page()
            try:
                page.goto(self.start_url, wait_until='domcontentloaded')
                while True:
                    page.wait_for_timeout(500)
                    try:
                        installed = page.evaluate('() => window.__sfFlowTestReady !== undefined')
                        if not installed:
                            page.evaluate(tr(TEST_READY_SCRIPT))
                        if page.evaluate('() => window.__sfFlowTestReady'):
                            break
                    except Exception:
                        if page.is_closed():
                            raise RuntimeError('msg.0173')
                locator = self._locator(page, selector_type, selector)
                actionable = self._actionable_matches(locator)
                count = len(actionable)
                if count == 1:
                    actionable[0].highlight()
                    page.wait_for_timeout(3000)
                return count
            finally:
                context.close()
                browser.close()

    @staticmethod
    def _locator(page: Any, selector_type: str, selector: str) -> Any:
        if selector_type == 'role':
            role, separator, name = selector.partition('|')
            return page.get_by_role(role.strip(), name=name.strip() if separator else None)
        if selector_type == 'label':
            return page.get_by_label(selector, exact=True)
        if selector_type == 'placeholder':
            return page.get_by_placeholder(selector, exact=True)
        if selector_type == 'text':
            return page.get_by_text(selector, exact=True)
        if selector_type == 'css':
            return page.locator(selector)
        if selector_type == 'xpath':
            return page.locator(f'xpath={selector}')
        raise ValueError('msg.0174')

    @staticmethod
    def _visible_matches(locator: Any) -> list[Any]:
        """元の DOM 順を維持したまま、表示中の一致要素だけを返す。"""
        return [locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible()]

    @classmethod
    def _actionable_matches(cls, locator: Any) -> list[Any]:
        # DOM に存在するだけでなく、表示中かつ最前面にある要素へ絞り込む。
        """表示領域内にあり、他要素に覆われていない一致要素だけを返す。"""
        return [item for item in cls._visible_matches(locator) if cls._is_topmost(item)]

    @staticmethod
    def _is_topmost(locator: Any) -> bool:
        return bool(locator.evaluate('element => {\n            const rect = element.getBoundingClientRect();\n            if (rect.width <= 0 || rect.height <= 0 ||\n                rect.right <= 0 || rect.bottom <= 0 ||\n                rect.left >= window.innerWidth || rect.top >= window.innerHeight) return false;\n            const left = Math.max(0, rect.left), right = Math.min(window.innerWidth, rect.right);\n            const top = Math.max(0, rect.top), bottom = Math.min(window.innerHeight, rect.bottom);\n            const points = [\n                [(left + right) / 2, (top + bottom) / 2],\n                [left + Math.min(3, (right - left) / 2), (top + bottom) / 2],\n                [right - Math.min(3, (right - left) / 2), (top + bottom) / 2],\n                [(left + right) / 2, top + Math.min(3, (bottom - top) / 2)],\n                [(left + right) / 2, bottom - Math.min(3, (bottom - top) / 2)]\n            ];\n            return points.some(([x, y]) => {\n                const hit = document.elementFromPoint(x, y);\n                return hit && (hit === element || element.contains(hit));\n            });\n        }'))


class _DebugPause(BaseException):
    """対象イベントの直前でデバッグ実行を正常停止するための内部通知。"""


class DebugBrowserSession:
    """固定スレッド上でブラウザーを保持し、選択と検証で現在ページを再利用する。"""

    def __init__(self, project_dir: Path, start_url: str, logger: Callable[[str, str], None],
                 storage_state_getter: Callable[[], Path | None] | None=None) -> None:
        self.project_dir = project_dir
        self.start_url = start_url
        self._log_sink = logger
        self.logger = lambda message: logger(message, self.__class__.__name__)
        self.storage_state_getter = storage_state_getter or (lambda: self.project_dir / 'data' / 'browser_state.json')
        self._tasks: queue.Queue[tuple[Callable[[], Any] | None, Future[Any]]] = queue.Queue()
        self._cancel_requested = threading.Event()
        self._thread = threading.Thread(target=self._worker, name='locator-debug-browser', daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            # 依存関係が不足していてもアプリ本体は起動し、操作時に画面へ理由を返す。
            while True:
                task, future = self._tasks.get()
                if task is None:
                    future.set_result(None)
                    return
                future.set_exception(RuntimeError('msg.0169'))
        playwright = sync_playwright().start()
        browser = context = page = None

        def dispose() -> None:
            nonlocal browser, context, page
            try:
                if context is not None:
                    context.close()
            finally:
                if browser is not None:
                    browser.close()
                browser = context = page = None

        def ensure_page(target_url: str='') -> tuple[Any, Any]:
            nonlocal browser, context, page
            if page is not None and not page.is_closed():
                return context, page
            state_path = self.storage_state_getter()
            browser = playwright.chromium.launch(channel='chrome', headless=False, args=['--start-maximized'])
            options: dict[str, Any] = {'no_viewport': True}
            if state_path is not None and state_path.exists():
                options['storage_state'] = str(state_path)
            context = browser.new_context(**options)
            page = context.new_page()
            page.goto(target_url or self.start_url, wait_until='domcontentloaded')
            return context, page

        self._ensure_page = ensure_page
        self._dispose = dispose
        while True:
            task, future = self._tasks.get()
            if task is None:
                try:
                    dispose()
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

    def open(self, target_url: str='') -> None:
        self._cancel_requested.clear()
        def task() -> None:
            _context, page = self._ensure_page(target_url)
            page.bring_to_front()
        self._submit(task)

    def pick(self, target_url: str='') -> dict[str, str]:
        self._cancel_requested.clear()

        def task() -> dict[str, str]:
            context, page = self._ensure_page(target_url)
            page.bring_to_front()
            picker = ElementPicker(self.project_dir, target_url or self.start_url)
            while True:
                if self._cancel_requested.is_set():
                    raise RuntimeError('msg.0170')
                page.wait_for_timeout(500)
                if not page.evaluate('() => window.__sfFlowPicked !== undefined'):
                    page.evaluate(tr(PICKER_SCRIPT))
                result = page.evaluate('() => window.__sfFlowPicked')
                if result:
                    if result.get('cancelled'):
                        raise RuntimeError('msg.0170')
                    state_path = self.storage_state_getter()
                    if state_path is not None:
                        state_path.parent.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(state_path))
                    return picker._choose_unique_locator(page, result)
        return self._submit(task)

    def test(self, selector_type: str, selector: str, target_url: str='') -> int:
        def task() -> int:
            _context, page = self._ensure_page(target_url)
            page.bring_to_front()
            picker = ElementPicker(self.project_dir, target_url or self.start_url)
            actionable = picker._actionable_matches(picker._locator(page, selector_type, selector))
            if len(actionable) == 1:
                actionable[0].highlight()
            return len(actionable)
        return self._submit(task)

    def execute_until(self, jobs: list[dict[str, Any]], target_event_id: int, variables: dict[str, str], target_url: str='') -> None:
        def task() -> None:
            from core.conditions import evaluate_guard
            from core.executor import WorkflowExecutor
            _context, page = self._ensure_page(target_url)
            page.goto(target_url or self.start_url, wait_until='domcontentloaded')
            artifact_dir = self.project_dir / 'artifacts' / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_debug')
            artifact_dir.mkdir(parents=True, exist_ok=True)
            executor = WorkflowExecutor(self.project_dir, lambda message: self._log_sink(message, 'WorkflowExecutor'))

            def pause_at_target(event: dict[str, Any]) -> None:
                if int(event.get('id', -1)) == target_event_id:
                    raise _DebugPause

            try:
                for index, job in enumerate(jobs, 1):
                    root_data = job.get('data')
                    if evaluate_guard(job.get('guard'), lambda path: executor._resolve_guard_data(root_data, path, {})):
                        executor._execute_workflow_on_page(page, job['events'], variables, artifact_dir, root_data, f'debug_{index}', on_event_start=pause_at_target)
            except _DebugPause:
                page.bring_to_front()
                return
            raise RuntimeError('msg.0359')
        self._submit(task)

    def close_browser(self) -> None:
        self._cancel_requested.set()
        self._submit(self._dispose)

    def shutdown(self) -> None:
        self._cancel_requested.set()
        future: Future[Any] = Future()
        self._tasks.put((None, future))
        try:
            future.result(timeout=5)
        except Exception:
            pass
