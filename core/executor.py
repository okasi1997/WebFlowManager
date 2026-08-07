"""Playwright を使用して、登録済みイベントを順番に実行する。"""
from __future__ import annotations
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from browser.page_runtime import active_page, browser_args, browser_context_options, close_browser_context, is_topmost, launch_persistent_chrome, locators_in_frames, open_pages, restore_storage_state, settle_new_page
from browser.profile_runtime import persistent_profile_dir
from core.conditions import decode_guard, evaluate_guard
from core.settings import SELECT_FIRST_VALUE
from i18n import tr
VARIABLE_PATTERN = re.compile('\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}')
DATA_REFERENCE_PATTERN = re.compile(r'\$\{data:([^{}]+)\}')
SALESFORCE_SPINNER_SELECTOR = '.slds-spinner, lightning-spinner'
POST_ACTION_SPINNER_CHECKS = {'click', 'select', 'press', 'goto', 'upload_file'}

def find_variables(events: list[dict[str, Any]]) -> list[str]:
    """外部入力が必要な変数だけを抽出する。get_text の生成変数は除外する。"""
    names: set[str] = set()
    produced: set[str] = set()
    for event in events:
        if event.get('action') == 'get_text':
            variable_name = str(event.get('value', '')).strip()
            if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', variable_name):
                produced.add(variable_name)
        for field in ('selector', 'value'):
            names.update(VARIABLE_PATTERN.findall(str(event.get(field, ''))))
    return sorted(names - produced)

def substitute(text: str, variables: dict[str, str]) -> str:

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise ValueError(f'Variable has no value: {name}')
        return variables[name]
    return VARIABLE_PATTERN.sub(replace, text)

class WorkflowExecutor:
    """一つのブラウザーセッション内でフロー群を実行する。"""

    def __init__(self, project_dir: Path, logger: Callable[[str], None]) -> None:
        self.project_dir = project_dir
        self.logger = lambda message: logger(tr(message))

    def run_batch(self, steps: list[dict[str, Any]], variables: dict[str, str], on_step_start: Callable[[dict[str, Any]], Any] | None=None, on_step_success: Callable[[dict[str, Any], Any], None] | None=None, on_step_failure: Callable[[dict[str, Any], Any, Exception], None] | None=None, on_event_start: Callable[[dict[str, Any], dict[str, Any]], None] | None=None, browser_visible: bool=True, session_name: str='batch', storage_state_path: Path | None | bool=False) -> None:
        """計画済みの全ステップを、一つの browser/context/page で実行する。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError('Playwright is not installed. Run: pip install -r requirements.txt') from error
        safe_session = re.sub('[^A-Za-z0-9_-]', '_', session_name)
        artifact_dir = self.project_dir / 'artifacts' / datetime.now().strftime('%Y%m%d_%H%M%S')
        state_path = self.project_dir / 'data' / 'browser_state.json' if storage_state_path is False else storage_state_path
        shared_session = session_name in {'batch', 'preamble'}
        output_state_path = state_path if shared_session else (state_path.parent / f'{state_path.stem}_{safe_session}.json' if state_path is not None else None)
        # 各並列組は同じログイン状態を読み込むが、終了時の書き込み先は分離する。
        # 複数スレッドによる browser_state.json の同時上書きを避けるためである。
        with sync_playwright() as playwright:
            # Chrome の永続ユーザーデータディレクトリは一つのプロセスが占有する。
            # 並列グループには個別のプロファイルを割り当てるが、選択中のログイン状態は
            # いずれのプロファイルにも同じ内容を復元する。
            profile_dir = persistent_profile_dir(
                self.project_dir,
                state_path if shared_session else output_state_path,
            )
            temporary_profile = None
            context = None
            if profile_dir is None:
                temporary_profile = tempfile.TemporaryDirectory(prefix='webflow_chrome_')
                profile_dir = Path(temporary_profile.name)
            try:
                context = launch_persistent_chrome(
                    playwright,
                    profile_dir,
                    visible=browser_visible,
                )
                restore_storage_state(context, state_path)
                pages = context.pages
                page = pages[-1] if pages else context.new_page()
                for step_number, step in enumerate(steps, 1):
                    token = on_step_start(step) if on_step_start else None
                    try:
                        record = step.get('record')
                        root_data = record.get('data') if record else None
                        workflow_guard = decode_guard(step.get('guard'))
                        if evaluate_guard(workflow_guard, lambda path: self._resolve_guard_data(root_data, path, {})):
                            self._execute_workflow_on_page(page, step['events'], variables, artifact_dir, root_data, f'step_{step_number}', 0, self._step_log_prefix(step), (lambda event, current=step: on_event_start(current, event)) if on_event_start else None)
                        else:
                            self.logger(f'{self._step_log_prefix(step)}msg.0406')
                    except Exception as error:
                        if on_step_failure:
                            on_step_failure(step, token, error)
                        raise
                    if on_step_success:
                        on_step_success(step, token)
            finally:
                active_error = sys.exc_info()[1]
                cleanup_error: Exception | None = None
                if active_error is not None and browser_visible and context is not None:
                    self.logger('msg.0564')
                    while True:
                        try:
                            pages = open_pages(context)
                            if not pages:
                                break
                            pages[-1].wait_for_timeout(250)
                        except Exception:
                            break
                try:
                    if context is not None and output_state_path is not None:
                        output_state_path.parent.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(output_state_path))
                except Exception as error:
                    cleanup_error = error
                try:
                    close_browser_context(context)
                except Exception as error:
                    cleanup_error = cleanup_error or error
                try:
                    if temporary_profile is not None:
                        temporary_profile.cleanup()
                except Exception as error:
                    cleanup_error = cleanup_error or error
                if cleanup_error is not None:
                    if active_error is None:
                        raise cleanup_error
                    try:
                        self.logger(f'Browser cleanup failed: {cleanup_error}')
                    except Exception:
                        pass

    @staticmethod
    def _browser_args(browser_visible: bool) -> list[str]:
        return browser_args(browser_visible)

    @staticmethod
    def _context_options(browser_visible: bool) -> dict[str, Any]:
        return browser_context_options(browser_visible)

    def _execute_workflow_on_page(self, page: Any, events: list[dict[str, Any]], variables: dict[str, str], artifact_dir: Path, root_data: dict[str, Any] | None, trace: str, start_index: int=0, log_prefix: str='', on_event_start: Callable[[dict[str, Any]], None] | None=None) -> None:
        enabled = [event for event in events if event.get('enabled', 1)][start_index:]
        self._execute_sequence(page, enabled, variables, artifact_dir, root_data, {}, trace, log_prefix, [], on_event_start)

    def _execute_sequence(self, page: Any, events: list[dict[str, Any]], variables: dict[str, str], artifact_dir: Path, root_data: dict[str, Any] | None, loop_context: dict[str, Any], trace: str, log_prefix: str='', loop_progress: list[str] | None=None, on_event_start: Callable[[dict[str, Any]], None] | None=None, deadline: float | None=None) -> None:
        # loop/retry は境界イベントを検出し、内側の配列を再帰的に実行する。
        loop_progress = loop_progress or []
        index = 0
        while index < len(events):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('msg.0576')
            page = active_page(page)
            event = events[index]
            action = event['action']
            if action in {'loop_start', 'loop_end', 'retry_start', 'retry_end', 'group_start', 'group_end'} and on_event_start:
                on_event_start(event)
            if action == 'group_start':
                end = self._matching_group_end(events, index)
                group_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
                if not evaluate_guard(group_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                    self.logger(f'{self._event_log_prefix(log_prefix, event, loop_progress)}msg.0406')
                    index = end + 1
                    continue
                path = str(event.get('data_path', '')).strip()
                iterations: list[tuple[dict[str, Any], list[str], str]] = [(loop_context, loop_progress, trace)]
                if path:
                    items = self._resolve_data(root_data, path, loop_context)
                    if not isinstance(items, list):
                        raise ValueError(f'msg.0180{path}msg.0181')
                    iterations = []
                    for item_number, item in enumerate(items, 1):
                        nested_context = dict(loop_context)
                        nested_context[path] = item
                        iterations.append((nested_context, [*loop_progress, f'{item_number}/{len(items)}'], f'{trace}_{item_number}'))
                retry_text = str(event.get('value', '')).strip()
                try:
                    retry_count = int(retry_text) if retry_text else int(event.get('retry_count', 0))
                    retry_interval_ms = int(event.get('retry_interval_ms', 0))
                    group_timeout_ms = int(event.get('timeout_ms', 10000))
                    if retry_count < 0 or retry_interval_ms < 0 or group_timeout_ms <= 0:
                        raise ValueError
                except ValueError as error:
                    raise ValueError(f"group '{event['name']}' has invalid execution control values") from error
                for nested_context, progress, nested_trace in iterations:
                    for attempt in range(1, retry_count + 2):
                        group_deadline = time.monotonic() + group_timeout_ms / 1000
                        if deadline is not None:
                            group_deadline = min(group_deadline, deadline)
                        try:
                            self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, nested_context, f'{nested_trace}_retry_{attempt}', log_prefix, progress, on_event_start, group_deadline)
                            break
                        except Exception as error:
                            if attempt > retry_count:
                                raise
                            group_prefix = self._event_log_prefix(log_prefix, event, progress)
                            self.logger(f'{group_prefix}msg.0568{attempt}/{retry_count + 1}msg.0569{error}')
                            if retry_interval_ms:
                                self.logger(f'{group_prefix}msg.0570{retry_interval_ms} ms')
                                page.wait_for_timeout(retry_interval_ms)
                index = end + 1
                continue
            if action == 'group_end':
                raise ValueError('group_end has no matching group_start')
            if action == 'loop_start':
                end = self._matching_loop_end(events, index)
                group_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
                if not evaluate_guard(group_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                    self.logger(f'{self._event_log_prefix(log_prefix, event, loop_progress)}msg.0406')
                    index = end + 1
                    continue
                path = str(event.get('data_path', ''))
                if not path:
                    raise ValueError(f"msg.0178{event['name']}msg.0179")
                items = self._resolve_data(root_data, path, loop_context)
                if not isinstance(items, list):
                    raise ValueError(f'msg.0180{path}msg.0181')
                event_prefix = self._event_log_prefix(log_prefix, event, loop_progress)
                self.logger(f'{event_prefix}msg.0182{path}msg.0183{len(items)}msg.0184')
                for item_number, item in enumerate(items, 1):
                    progress = [*loop_progress, f'{item_number}/{len(items)}']
                    self.logger(f'{self._event_log_prefix(log_prefix, event, progress)}msg.0182{path} [{item_number}/{len(items)}]')
                    nested_context = dict(loop_context)
                    nested_context[path] = item
                    self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, nested_context, f'{trace}_{item_number}', log_prefix, progress, on_event_start, deadline)
                index = end + 1
                continue
            if action == 'loop_end':
                raise ValueError('msg.0185')
            if action == 'retry_start':
                end = self._matching_retry_end(events, index)
                group_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
                if not evaluate_guard(group_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                    self.logger(f'{self._event_log_prefix(log_prefix, event, loop_progress)}msg.0406')
                    index = end + 1
                    continue
                try:
                    retry_count = int(str(event.get('value', '')).strip())
                except ValueError as error:
                    raise ValueError(f"retry_start '{event['name']}' requires a non-negative integer value") from error
                if retry_count < 0:
                    raise ValueError(f"retry_start '{event['name']}' requires a non-negative integer value")
                total_attempts = retry_count + 1
                for attempt in range(1, total_attempts + 1):
                    retry_prefix = self._event_log_prefix(log_prefix, event, loop_progress)
                    self.logger(f'{retry_prefix}msg.0186{attempt}/{total_attempts}]')
                    try:
                        self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, loop_context, f'{trace}_retry_{attempt}', log_prefix, loop_progress, on_event_start, deadline)
                        break
                    except Exception:
                        if attempt >= total_attempts:
                            self.logger(f'{retry_prefix}msg.0187{retry_count}msg.0188')
                            raise
                        self.logger(f'{retry_prefix}msg.0189{attempt + 1}/{total_attempts}]')
                index = end + 1
                continue
            if action == 'retry_end':
                raise ValueError('retry_end has no matching retry_start')
            event_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
            if not evaluate_guard(event_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                self.logger(f'{self._event_log_prefix(log_prefix, event, loop_progress)}msg.0406')
                index += 1
                continue
            if on_event_start:
                on_event_start(event)
            effective = dict(event)
            if deadline is not None:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                effective['timeout_ms'] = min(int(effective.get('timeout_ms', 10000)), remaining_ms)
            data_path = str(event.get('data_path', ''))
            if data_path and event.get('action') != 'get_text':
                effective['value'] = str(self._resolve_data(root_data, data_path, loop_context))
            for field in ('selector', 'fallback_selector', 'value', 'failure_target'):
                effective[field] = self._substitute_data_references(
                    str(effective.get(field, '')),
                    root_data,
                    loop_context,
                )
            prefix = self._event_log_prefix(log_prefix, event, loop_progress)
            try:
                detail = self._event_log_detail(effective, variables)
            except ValueError:
                detail = action
            self.logger(
                f"{prefix}msg.0191{event['name']}"
                + (f' | {detail}' if detail else '')
                + (f' <- {data_path}' if data_path else '')
            )
            try:
                retry_count = max(0, int(event.get('retry_count', 0)))
                retry_interval_ms = max(0, int(event.get('retry_interval_ms', 0)))
                captured = None
                for attempt in range(1, retry_count + 2):
                    try:
                        self._wait_for_salesforce_spinner_if_present(page, int(effective.get('timeout_ms', 10000)), prefix)
                        captured = self._execute_event(page, effective, variables, artifact_dir)
                        # 文字入力は通常、画面遷移や読込を開始しないため、頻繁に行われる
                        # フォーム入力では操作後の画面・iframe 全体の再走査を省略する。
                        if action in POST_ACTION_SPINNER_CHECKS:
                            self._wait_for_salesforce_spinner_if_present(page, int(effective.get('timeout_ms', 10000)), prefix)
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError('msg.0576')
                        break
                    except Exception as attempt_error:
                        if attempt > retry_count:
                            raise
                        self.logger(f'{prefix}msg.0568{attempt}/{retry_count + 1}msg.0569{attempt_error}')
                        if retry_interval_ms:
                            self.logger(f'{prefix}msg.0570{retry_interval_ms} ms')
                            page.wait_for_timeout(retry_interval_ms)
                if action == 'get_text' and data_path:
                    self._assign_data(root_data, data_path, loop_context, captured)
            except Exception as error:
                try:
                    page = active_page(page)
                    failure_url = str(page.url)
                except Exception:
                    failure_url = ''
                safe_trace = re.sub('[^A-Za-z0-9_-]', '_', trace)
                screenshot = artifact_dir / f"error_{safe_trace}_{event['id']}.png"
                artifact_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
                self.logger(f'{prefix}msg.0192{error}')
                if failure_url:
                    self.logger(f'{prefix}Failure URL: {failure_url}')
                self.logger(f'{prefix}msg.0193{screenshot}')
                failure_action = str(event.get('failure_action', 'none'))
                if failure_action == 'refresh':
                    self.logger(f'{prefix}msg.0430')
                    page.reload(wait_until='domcontentloaded')
                elif failure_action == 'goto':
                    target = substitute(str(effective.get('failure_target', '')), variables)
                    self.logger(f'{prefix}msg.0431{target}')
                    page.goto(target, wait_until='domcontentloaded')
                if not event.get('continue_on_error', 0):
                    raise
            index += 1

    @staticmethod
    def _step_log_prefix(step: dict[str, Any]) -> str:
        workflow = f"msg.0194{step.get('position', '?')}]"
        if step.get('phase') == 'once':
            return workflow
        group = f"msg.0195{step.get('group', '1')}]"
        return f"{group}[Data {step.get('pcl_index', '?')}/{step.get('pcl_total', '?')}]{workflow}"

    @staticmethod
    def _event_log_prefix(log_prefix: str, event: dict[str, Any], loop_progress: list[str]) -> str:
        loops = ''.join((f'msg.0196{progress}]' for progress in loop_progress))
        return f"{log_prefix}msg.0197{event.get('position', '?')}]{loops}"

    @staticmethod
    def _event_log_detail(event: dict[str, Any], variables: dict[str, str]) -> str:
        """実際に使用する操作パラメーターを簡潔なログ文字列として返す。"""
        action = str(event.get('action', ''))
        selector_type = str(event.get('selector_type', 'none'))
        selector = substitute(str(event.get('selector', '')), variables)
        value = substitute(str(event.get('value', '')), variables)

        def clean(text: str) -> str:
            return ' '.join(text.splitlines())

        parts = [action]
        if selector_type != 'none' and selector:
            parts.append(f'selector[{selector_type}]="{clean(selector)}"')
        if action == 'goto':
            parts.append(f'url="{clean(value)}"')
        elif action == 'fill':
            parts.append(f'value="{clean(value)}"')
        elif action == 'select':
            parts.append('index=0' if value == SELECT_FIRST_VALUE else f'value="{clean(value)}"')
        elif action == 'press':
            parts.append(f'key="{clean(value)}"')
        elif action == 'upload_file':
            parts.append(f'file="{clean(value)}"')
        elif action == 'pause':
            parts.append(f'ms={clean(value)}')
        elif action == 'get_text' and value:
            parts.append(f'output={clean(value)}')
        elif action == 'screenshot' and value:
            parts.append(f'file="{clean(value)}"')
        return ' '.join(parts)

    @staticmethod
    def _matching_loop_end(events: list[dict[str, Any]], start: int) -> int:
        depth = 0
        for index in range(start + 1, len(events)):
            if events[index]['action'] == 'loop_start':
                depth += 1
            elif events[index]['action'] == 'loop_end':
                if depth == 0:
                    return index
                depth -= 1
        raise ValueError(f"msg.0178{events[start]['name']}msg.0198")

    @staticmethod
    def _matching_retry_end(events: list[dict[str, Any]], start: int) -> int:
        depth = 0
        for index in range(start + 1, len(events)):
            if events[index]['action'] == 'retry_start':
                depth += 1
            elif events[index]['action'] == 'retry_end':
                if depth == 0:
                    return index
                depth -= 1
        raise ValueError(f"retry_start '{events[start]['name']}' is missing retry_end")

    @staticmethod
    def _matching_group_end(events: list[dict[str, Any]], start: int) -> int:
        depth = 0
        for index in range(start + 1, len(events)):
            if events[index]['action'] == 'group_start':
                depth += 1
            elif events[index]['action'] == 'group_end':
                if depth == 0:
                    return index
                depth -= 1
        raise ValueError(f"group '{events[start]['name']}' is missing group_end")

    @staticmethod
    def _resolve_data(root_data: dict[str, Any] | None, path: str, loop_context: dict[str, Any]) -> Any:
        if root_data is None:
            raise ValueError(f'msg.0199{path}msg.0200')
        current: Any = root_data
        prefix: list[str] = []
        for part in path.split('.'):
            prefix.append(part)
            current_path = '.'.join(prefix)
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f'msg.0201{current_path}')
            current = current[part]
            if isinstance(current, list) and current_path in loop_context:
                current = loop_context[current_path]
            elif isinstance(current, list) and current_path != path:
                raise ValueError(f'msg.0202{current_path}msg.0203')
        return current

    @classmethod
    def _resolve_guard_data(cls, root_data: dict[str, Any] | None, path: str, loop_context: dict[str, Any]) -> Any:
        try:
            return cls._resolve_data(root_data, path, loop_context)
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _substitute_data_references(
        cls,
        text: str,
        root_data: dict[str, Any] | None,
        loop_context: dict[str, Any],
    ) -> str:
        """${data:path} を現在のレコードにある単一値で置換する。"""
        def replace(match: re.Match[str]) -> str:
            path = match.group(1).strip()
            if not path:
                raise ValueError('Data reference path is empty')
            value = cls._resolve_data(root_data, path, loop_context)
            if isinstance(value, (dict, list)):
                raise ValueError(f'Data reference must point to a scalar value: {path}')
            return '' if value is None else str(value)

        return DATA_REFERENCE_PATTERN.sub(replace, text)

    @staticmethod
    def _assign_data(root_data: dict[str, Any] | None, path: str, loop_context: dict[str, Any], value: Any) -> None:
        """取得した値を、現在処理中の PCL または list 要素へ書き戻す。"""
        if root_data is None:
            raise ValueError(f'msg.0199{path}msg.0200')
        current: Any = root_data
        prefix: list[str] = []
        parts = path.split('.')
        for index, part in enumerate(parts):
            prefix.append(part)
            current_path = '.'.join(prefix)
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f'msg.0201{current_path}')
            if index == len(parts) - 1:
                if isinstance(current[part], (dict, list)):
                    raise ValueError(f'msg.0201{current_path}')
                current[part] = value
                return
            current = current[part]
            if isinstance(current, list):
                if current_path not in loop_context:
                    raise ValueError(f'msg.0202{current_path}msg.0203')
                current = loop_context[current_path]

    def _locator(self, page: Any, selector_type: str, selector: str) -> Any:
        if selector_type == 'css':
            return page.locator(selector)
        if selector_type == 'text':
            return page.get_by_text(selector, exact=True)
        if selector_type == 'label':
            return page.get_by_label(selector, exact=True)
        if selector_type == 'placeholder':
            return page.get_by_placeholder(selector, exact=True)
        if selector_type == 'xpath':
            return page.locator(f'xpath={selector}')
        if selector_type == 'role':
            role, separator, name = selector.partition('|')
            return page.get_by_role(role.strip(), name=name.strip() if separator else None)
        raise ValueError(f'Action requires a selector: {selector_type}')

    def _locators(self, page: Any, selector_type: str, selector: str) -> list[Any]:
        return locators_in_frames(page, lambda frame: self._locator(frame, selector_type, selector))

    @staticmethod
    def _is_transient_target_error(error: Exception) -> bool:
        """動的な画面遷移で page／frame の参照が無効になったかを判定する。"""
        message = str(error).casefold()
        return any(fragment in message for fragment in (
            'target page, context or browser has been closed',
            'frame was detached',
            'execution context was destroyed',
            'cannot find context with specified id',
        ))

    def _locator_matches(self, page: Any, selector_type: str, selector: str) -> list[Any]:
        """一回の試行内で frame を取得し、その Locator をすべて評価する。"""
        locators = self._locators(page, selector_type, selector)
        return [
            locator.nth(index)
            for locator in locators
            for index in range(locator.count())
        ]

    def _unique_locator(self, page: Any, selector_type: str, selector: str, timeout: int=10000) -> Any:
        deadline = time.monotonic() + timeout / 1000
        all_matches: list[Any] = []
        while time.monotonic() < deadline:
            try:
                page = active_page(page)
                if page.is_closed():
                    raise RuntimeError('Target page has been closed')
                all_matches = self._locator_matches(page, selector_type, selector)
            except Exception as error:
                if not self._is_transient_target_error(error) and 'target page has been closed' not in str(error).casefold():
                    raise
                time.sleep(0.1)
                continue
            if all_matches:
                break
            page.wait_for_timeout(100)
        try:
            visible = [item for item in all_matches if item.is_visible()]
        except Exception as error:
            if self._is_transient_target_error(error):
                return self._unique_locator(page, selector_type, selector, max(1, int((deadline - time.monotonic()) * 1000)))
            raise
        # 画面内に一件だけ見える場合でも、画面外に同じ要素があれば一意とは扱わない。
        # 主定位を失敗させ、保存済みの正確な予備 XPath を使用できるようにする。
        if len(all_matches) == 1 and len(visible) == 1:
            visible[0].scroll_into_view_if_needed(timeout=max(1, timeout))
        actionable = [item for item in visible if is_topmost(item)] if len(all_matches) == 1 else []
        if len(actionable) != 1:
            raise RuntimeError(f'msg.0204{len(all_matches)}msg.0205{len(visible)}msg.0206{len(actionable)}msg.0073')
        return actionable[0]

    def _execute_event(self, page: Any, event: dict[str, Any], variables: dict[str, str], artifact_dir: Path) -> Any:
        page = active_page(page)
        action = event['action']
        selector = substitute(str(event.get('selector', '')), variables)
        fallback_selector = substitute(str(event.get('fallback_selector', '')), variables)
        value = substitute(str(event.get('value', '')), variables)
        timeout = int(event.get('timeout_ms', 10000))
        page.set_default_timeout(timeout)
        if action == 'goto':
            page.goto(value, wait_until='domcontentloaded')
        elif action == 'click':
            previous_pages = set(open_pages(page.context))
            self._event_locator(page, event, selector, fallback_selector, timeout).click()
            settle_new_page(page, previous_pages, timeout)
        elif action == 'fill':
            self._event_locator(page, event, selector, fallback_selector, timeout).fill(value)
        elif action == 'select':
            locator = self._event_locator(page, event, selector, fallback_selector, timeout)
            if value == SELECT_FIRST_VALUE:
                locator.select_option(index=0)
            else:
                locator.select_option(value)
        elif action == 'wait':
            self._event_locator(page, event, selector, fallback_selector, timeout)
        elif action == 'wait_hidden':
            self._wait_until_hidden(page, event['selector_type'], selector, timeout)
        elif action == 'press':
            self._event_locator(page, event, selector, fallback_selector, timeout).press(value)
        elif action == 'upload_file':
            file_path = Path(value)
            if not file_path.is_absolute():
                file_path = self.project_dir / file_path
            file_path = file_path.resolve()
            if not file_path.is_file():
                raise ValueError(f'msg.0421{file_path}')
            self._file_input_locator(page, event, selector, fallback_selector, timeout).set_input_files(str(file_path))
        elif action == 'get_text':
            variable_name = value.strip()
            if variable_name and not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', variable_name):
                raise ValueError('msg.0210')
            locator = self._event_locator(page, event, selector, fallback_selector, timeout)
            captured = (locator.text_content() or '').strip()
            if variable_name:
                variables[variable_name] = captured
            return captured
        elif action == 'screenshot':
            filename = value or f"screenshot_{event['id']}.png"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifact_dir / filename), full_page=True)
        elif action == 'pause':
            page.wait_for_timeout(int(value or timeout))
        else:
            raise ValueError(f'Unsupported action: {action}')

    def _wait_until_hidden(self, page: Any, selector_type: str, selector: str, timeout: int=10000) -> None:
        """画面と配下 frame にある一致要素がすべて非表示または切断されるまで待機する。"""
        deadline = time.monotonic() + timeout / 1000
        while True:
            page = active_page(page)
            visible_count = self._visible_locator_count(page, selector_type, selector)
            if visible_count == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f'msg.0566{visible_count}msg.0567')
            page.wait_for_timeout(100)

    def _wait_for_salesforce_spinner_if_present(self, page: Any, timeout: int, prefix: str='') -> None:
        """Spinner がない画面を遅延させず、Salesforce 共通の読込完了待ちを行う。"""
        page = active_page(page)
        visible_count = self._visible_locator_count(page, 'css', SALESFORCE_SPINNER_SELECTOR)
        if visible_count == 0:
            return
        self.logger(f'{prefix}msg.0574{visible_count}msg.0567')
        self._wait_until_hidden(page, 'css', SALESFORCE_SPINNER_SELECTOR, timeout)

    def _visible_locator_count(self, page: Any, selector_type: str, selector: str) -> int:
        deadline = time.monotonic() + 1.0
        while True:
            try:
                page = active_page(page)
                if page.is_closed():
                    raise RuntimeError('Target page has been closed')
                return sum(item.is_visible() for item in self._locator_matches(page, selector_type, selector))
            except Exception as error:
                transient = (
                    self._is_transient_target_error(error)
                    or 'target page has been closed' in str(error).casefold()
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def _event_locator(self, page: Any, event: dict[str, Any], selector: str, fallback_selector: str, timeout: int=10000) -> Any:
        try:
            return self._unique_locator(page, event['selector_type'], selector, timeout)
        except RuntimeError:
            fallback_type = str(event.get('fallback_selector_type', 'none'))
            if fallback_type == 'none' or not fallback_selector:
                raise
            self.logger(f'msg.0212{fallback_type}: "{fallback_selector}"')
            try:
                return self._unique_locator(page, fallback_type, fallback_selector, timeout)
            except Exception as fallback_error:
                raise RuntimeError(f'msg.0565{fallback_error}') from fallback_error

    def _file_input_locator(self, page: Any, event: dict[str, Any], selector: str, fallback_selector: str, timeout: int=10000) -> Any:
        """非表示の場合もある file input を可視性判定なしで一意に取得する。"""
        deadline = time.monotonic() + timeout / 1000
        matches: list[Any] = []
        while time.monotonic() < deadline:
            page = active_page(page)
            locators = self._locators(page, event['selector_type'], selector)
            matches = [locator.nth(index) for locator in locators for index in range(locator.count())]
            if matches:
                break
            page.wait_for_timeout(100)
        if len(matches) == 1:
            return matches[0]
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        if fallback_type != 'none' and fallback_selector:
            fallbacks = self._locators(page, fallback_type, fallback_selector)
            fallback_matches = [locator.nth(index) for locator in fallbacks for index in range(locator.count())]
            if len(fallback_matches) == 1:
                return fallback_matches[0]
        raise RuntimeError(f'msg.0423{len(matches)}')
