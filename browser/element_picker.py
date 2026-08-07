"""ブラウザー上で要素を選択し、一意な locator 候補を生成する。"""
from __future__ import annotations
import json
import queue
import tempfile
import threading
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from browser.page_runtime import active_page, bring_page_to_front, close_browser_context, is_topmost, launch_persistent_chrome, locators_in_frames, page_frames, restore_storage_state
from browser.picker_scripts import picker_script
from browser.profile_runtime import persistent_profile_dir
from i18n import tr

class ElementPicker:
    """F2 で選択モードへ入り、操作可能な要素だけを候補として返す。"""

    def _choose_unique_locator(self, page: Any, info: dict[str, str], picked_frame: Any | None=None) -> dict[str, str]:
        # 人が理解しやすい locator から順に試し、XPath は予備として保持する。
        action = 'fill' if info.get('role') in ('textbox', 'combobox') else 'click'
        display = info.get('label') or info.get('name') or info.get('text') or info.get('tag')
        candidates: list[tuple[str, str]] = []
        if info.get('label'):
            candidates.append(('label', info['label']))
        if info.get('role') and info.get('name'):
            candidates.append(('role', f"{info['role']}|{info['name']}"))
        if info.get('placeholder'):
            candidates.append(('placeholder', info['placeholder']))
        if info.get('text'):
            candidates.append(('text', info['text']))
        if info.get('css'):
            candidates.append(('css', info['css']))
        if info.get('xpath'):
            candidates.append(('xpath', info['xpath']))
        for selector_type, selector in candidates:
            # 可視性は現在のスクロール位置に左右される。現在操作可能な一致が一件でも、
            # label／text が画面外の複数要素を指す場合があるため、DOM 全体で一意な
            # 定位だけを採用し、実行時の表示位置による対象の入れ替わりを防ぐ。
            matches = self._matches_in_page(page, selector_type, selector)
            if len(matches) != 1:
                continue
            actionable = self._actionable_matches_in_page(page, selector_type, selector)
            if len(actionable) == 1:
                return {'selector_type': selector_type, 'selector': selector, 'fallback_selector_type': 'xpath' if selector_type != 'xpath' and info.get('xpath') else 'none', 'fallback_selector': info.get('xpath', '') if selector_type != 'xpath' else '', 'iframe_path': self._iframe_path(picked_frame), 'display': display, 'suggested_action': action, 'match_count': '1'}
        raise RuntimeError('msg.0172')

    @staticmethod
    def _iframe_path(frame: Any | None) -> str:
        """対象 frame までの各 iframe を、親 frame 内で一意な CSS として保存する。"""
        selectors: list[str] = []
        current = frame
        while current is not None and current.parent_frame is not None:
            element = current.frame_element()
            selector = element.evaluate("""element => {
                const escape = value => CSS.escape(String(value));
                const unique = selector => element.ownerDocument.querySelectorAll(selector).length === 1;
                for (const name of ['id', 'name', 'title']) {
                    const value = element.getAttribute(name);
                    if (!value) continue;
                    const selector = name === 'id'
                        ? `#${escape(value)}`
                        : `${element.localName}[${name}="${escape(value)}"]`;
                    if (unique(selector)) return selector;
                }
                const segment = node => {
                    const siblings = Array.from(node.parentElement.children)
                        .filter(item => item.localName === node.localName);
                    return `${node.localName}:nth-of-type(${siblings.indexOf(node) + 1})`;
                };
                let node = element;
                let selector = segment(node);
                while (!unique(selector) && node.parentElement && node.parentElement !== element.ownerDocument.documentElement) {
                    node = node.parentElement;
                    selector = `${segment(node)} > ${selector}`;
                }
                return selector;
            }""")
            selectors.append(str(selector))
            current = current.parent_frame
        selectors.reverse()
        return json.dumps(selectors, ensure_ascii=False)

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

    @classmethod
    def _actionable_matches_in_page(cls, page: Any, selector_type: str, selector: str) -> list[Any]:
        locators = locators_in_frames(page, lambda frame: cls._locator(frame, selector_type, selector))
        return [item for locator in locators for item in cls._actionable_matches(locator)]

    @classmethod
    def _matches_in_page(cls, page: Any, selector_type: str, selector: str) -> list[Any]:
        locators = locators_in_frames(page, lambda frame: cls._locator(frame, selector_type, selector))
        return [
            locator.nth(index)
            for locator in locators
            for index in range(locator.count())
        ]

    @staticmethod
    def _visible_matches(locator: Any) -> list[Any]:
        """元の DOM 順を維持したまま、表示中の一致要素だけを返す。"""
        return [locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible()]

    @classmethod
    def _actionable_matches(cls, locator: Any) -> list[Any]:
        # DOM に存在するだけでなく、表示中かつ最前面にある要素へ絞り込む。
        """表示領域内にあり、他要素に覆われていない一致要素だけを返す。"""
        return [item for item in cls._visible_matches(locator) if is_topmost(item)]


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
        context = page = None
        temporary_profile: tempfile.TemporaryDirectory[str] | None = None

        def dispose() -> None:
            nonlocal context, page, temporary_profile
            try:
                close_browser_context(context)
            finally:
                context = page = None
                if temporary_profile is not None:
                    temporary_profile.cleanup()
                    temporary_profile = None

        def ensure_page(target_url: str='') -> tuple[Any, Any]:
            nonlocal context, page, temporary_profile
            if context is not None and page is not None:
                try:
                    page = active_page(page)
                    if not page.is_closed():
                        return context, page
                except Exception:
                    # アプリ外で Chrome が閉じられた可能性があるため、
                    # 再起動前に無効な Playwright の参照を破棄する。
                    dispose()
            state_path = self.storage_state_getter()
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    user_data_dir = persistent_profile_dir(self.project_dir, state_path)
                    if user_data_dir is None:
                        temporary_profile = tempfile.TemporaryDirectory(prefix='webflow_chrome_')
                        user_data_dir = Path(temporary_profile.name)
                    context = launch_persistent_chrome(playwright, user_data_dir, visible=True)
                    restore_storage_state(context, state_path)
                    pages = context.pages
                    page = pages[-1] if pages else context.new_page()
                    page.goto(target_url or self.start_url, wait_until='domcontentloaded')
                    return context, page
                except Exception as error:
                    last_error = error
                    # 起動途中の context を残すと次回呼出しで再利用されるため必ず破棄する。
                    dispose()
                    if attempt == 0:
                        continue
            assert last_error is not None
            raise last_error

        self._ensure_page = ensure_page
        self._dispose = dispose
        while True:
            try:
                task, future = self._tasks.get(timeout=0.1)
            except queue.Empty:
                # ブラウザーを手動操作している間も新規タブの Target を再開できるよう、
                # 同期 API を定期的に呼び出して Playwright のイベントを処理する。
                try:
                    if page is not None and not page.is_closed():
                        page.wait_for_timeout(50)
                except Exception:
                    pass
                continue
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

    def _submit(self, task: Callable[[], Any], timeout: float | None=None) -> Any:
        future: Future[Any] = Future()
        self._tasks.put((task, future))
        return future.result(timeout=timeout)

    def open(self, target_url: str='') -> None:
        self._cancel_requested.clear()
        def task() -> None:
            _context, page = self._ensure_page(target_url)
            bring_page_to_front(page)
        self._submit(task)

    def pick(self, target_url: str='') -> dict[str, str]:
        self._cancel_requested.clear()

        def task() -> dict[str, str]:
            _context, page = self._ensure_page(target_url)
            bring_page_to_front(page)
            picker = ElementPicker()
            run_id = uuid.uuid4().hex
            script = picker_script(run_id, tr('msg.0490'), tr('msg.0491'))
            while True:
                if self._cancel_requested.is_set():
                    raise RuntimeError('msg.0170')
                page = active_page(page)
                page.wait_for_timeout(100)
                for frame in page_frames(page):
                    try:
                        installed_run_id = frame.evaluate(
                            '() => window.__webFlowPickerRunId || ""'
                        )
                        if installed_run_id != run_id:
                            frame.evaluate(script)
                        result = frame.evaluate('() => window.__sfFlowPicked')
                    except Exception:
                        continue
                    if result:
                        if result.get('cancelled'):
                            raise RuntimeError('msg.0170')
                        return picker._choose_unique_locator(page, result, frame)
        return self._submit(task)

    def test(self, selector_type: str, selector: str, target_url: str='') -> int:
        def task() -> int:
            _context, page = self._ensure_page(target_url)
            page = active_page(page)
            bring_page_to_front(page)
            picker = ElementPicker()
            actionable = picker._actionable_matches_in_page(page, selector_type, selector)
            if len(actionable) == 1:
                actionable[0].highlight()
            return len(actionable)
        return self._submit(task)

    def execute_event(self, event: dict[str, Any], target_url: str='') -> None:
        """現在のページで編集中のイベントを一度だけ実行する。"""
        def task() -> None:
            from core.executor import WorkflowExecutor
            _context, page = self._ensure_page(target_url)
            page = active_page(page)
            bring_page_to_front(page)
            artifact_dir = self.project_dir / 'artifacts' / datetime.now().strftime('%Y%m%d_%H%M%S')
            executor = WorkflowExecutor(
                self.project_dir,
                lambda message: self._log_sink(message, 'WorkflowExecutor'),
            )
            executor._execute_event(page, event, {}, artifact_dir)
        self._submit(task)

    def execute_until(self, jobs: list[dict[str, Any]], target_event_id: int, variables: dict[str, str], target_url: str='') -> None:
        def task() -> None:
            from core.conditions import evaluate_guard
            from core.executor import WorkflowExecutor
            _context, page = self._ensure_page(target_url)
            page = active_page(page)
            page.goto(target_url or self.start_url, wait_until='domcontentloaded')
            artifact_dir = self.project_dir / 'artifacts' / datetime.now().strftime('%Y%m%d_%H%M%S')
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
                page = active_page(page)
                bring_page_to_front(page)
                return
            raise RuntimeError('msg.0359')
        self._submit(task)

    def close_browser(self) -> None:
        self._cancel_requested.set()
        # ワーカー初期化前に _dispose を参照すると競合するため実行時に解決する。
        try:
            self._submit(lambda: self._dispose(), timeout=10)
        except FutureTimeoutError as error:
            raise RuntimeError('msg.0562') from error

    def shutdown(self) -> None:
        self._cancel_requested.set()
        future: Future[Any] = Future()
        self._tasks.put((None, future))
        try:
            future.result(timeout=5)
        except Exception:
            pass
