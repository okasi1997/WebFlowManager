"""Playwright を使用して、登録済みイベントを順番に実行する。"""
from __future__ import annotations
import json
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from browser.page_runtime import active_page, browser_args, browser_context_options, close_browser_context, is_topmost, launch_persistent_chrome, open_pages, page_frames, restore_storage_state, settle_new_page
from browser.locators import build_locator, locators_across_frames
from browser.profile_runtime import persistent_profile_dir
from core.conditions import decode_guard, evaluate_guard
from core.settings import SELECT_FIRST_VALUE
from i18n import tr
VARIABLE_PATTERN = re.compile('\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}')
DATA_REFERENCE_PATTERN = re.compile(r'\$\{data:([^{}]+)\}')
SALESFORCE_SPINNER_SELECTOR = '.slds-spinner, lightning-spinner'
SPINNER_TRIGGER_ACTIONS = {'click', 'select', 'goto', 'upload_file'}

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
        self._input_frame_cache: tuple[Any, Any] | None = None
        self._saved_frame_cache: dict[tuple[Any, str], Any] = {}
        self._spinner_observed_frames: set[Any] = set()

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
        self._execute_sequence(page, events[start_index:], variables, artifact_dir, root_data, {}, trace, log_prefix, [], on_event_start)

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
            if not event.get('enabled', 1):
                # 無効な境界だけを先に除外すると、配下の有効イベントがグループ外として
                # 実行されてしまう。開始境界が無効な場合は対応する終了境界まで一括で飛ばす。
                if action == 'group_start':
                    index = self._matching_group_end(events, index) + 1
                elif action == 'loop_start':
                    index = self._matching_loop_end(events, index) + 1
                elif action == 'retry_start':
                    index = self._matching_retry_end(events, index) + 1
                else:
                    index += 1
                continue
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
                        spinner_baseline = (
                            self._prepare_spinner_observers(page, effective)
                            if self._requires_spinner_check(effective)
                            else {}
                        )
                        captured = self._execute_event(page, effective, variables, artifact_dir)
                        if spinner_baseline:
                            self._wait_for_observed_spinner(
                                page,
                                spinner_baseline,
                                int(effective.get('timeout_ms', 10000)),
                                prefix,
                                effective,
                            )
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
        return build_locator(page, selector_type, selector)

    def _locators(self, page: Any, selector_type: str, selector: str) -> list[Any]:
        return locators_across_frames(page, selector_type, selector)

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
        return [
            locator.nth(index)
            for locator in self._locators(page, selector_type, selector)
            for index in range(locator.count())
        ]

    def _unique_locator(
        self, page: Any, selector_type: str, selector: str,
        timeout: int=10000, require_actionable: bool=True,
    ) -> Any:
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
        if not require_actionable and len(all_matches) == 1:
            return all_matches[0]
        try:
            visible = [item for item in all_matches if item.is_visible()]
        except Exception as error:
            if self._is_transient_target_error(error):
                return self._unique_locator(
                    page, selector_type, selector,
                    max(1, int((deadline - time.monotonic()) * 1000)),
                    require_actionable,
                )
            raise
        # 画面内に一件だけ見える場合でも、画面外に同じ要素があれば一意とは扱わない。
        # 主定位を失敗させ、保存済みの正確な予備 XPath を使用できるようにする。
        if len(all_matches) == 1 and len(visible) == 1:
            visible[0].scroll_into_view_if_needed(timeout=max(1, timeout))
        actionable = [item for item in visible if is_topmost(item)] if len(all_matches) == 1 else []
        if len(actionable) != 1:
            raise RuntimeError(f'msg.0204{len(all_matches)}msg.0205{len(visible)}msg.0206{len(actionable)}msg.0073')
        return actionable[0]

    def _unique_locator_in_frame(
        self, page: Any, frame: Any, selector_type: str, selector: str,
        timeout: int=10000, require_actionable: bool=True,
    ) -> Any:
        """指定された frame の外へ探索範囲を広げず、一意な要素が現れるまで待機する。"""
        deadline = time.monotonic() + timeout / 1000
        matches: list[Any] = []
        visible: list[Any] = []
        actionable: list[Any] = []
        while True:
            try:
                locator = self._locator(frame, selector_type, selector)
                matches = [locator.nth(index) for index in range(locator.count())]
                if len(matches) == 1 and not require_actionable:
                    return matches[0]
                visible = [item for item in matches if item.is_visible()]
                if len(matches) == 1 and len(visible) == 1:
                    visible[0].scroll_into_view_if_needed(
                        timeout=max(1, int((deadline - time.monotonic()) * 1000)),
                    )
                    actionable = [visible[0]] if is_topmost(visible[0]) else []
                    if actionable:
                        return actionable[0]
                else:
                    actionable = []
            except Exception as error:
                if not self._is_transient_target_error(error):
                    raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f'msg.0204{len(matches)}msg.0205{len(visible)}'
                    f'msg.0206{len(actionable)}msg.0073'
                )
            active_page(page).wait_for_timeout(100)

    def _cached_input_locator(self, page: Any, selector_type: str, selector: str, timeout: int) -> Any | None:
        """連続した軽量操作時、直前に確認済みの frame 内だけを高速に検索する。"""
        if self._input_frame_cache is None:
            return None
        cached_page, frame = self._input_frame_cache
        page = active_page(page)
        try:
            if cached_page is not page or frame not in page.frames or frame.is_detached():
                self._input_frame_cache = None
                return None
            locator = self._locator(frame, selector_type, selector)
            if locator.count() != 1:
                return None
            # fill() 自身が可視性、スクロール、編集可能性を待機する。
            return locator.nth(0)
        except Exception:
            self._input_frame_cache = None
            return None

    def _remember_input_frame(self, page: Any, locator: Any) -> None:
        """Playwright Locator が属する frame を次の連続入力用に保持する。"""
        frame = getattr(locator, '_frame', None)
        if frame is None:
            impl_frame = getattr(getattr(locator, '_impl_obj', None), '_frame', None)
            try:
                frame = next(
                    candidate for candidate in active_page(page).frames
                    if getattr(candidate, '_impl_obj', None) is impl_frame
                )
            except (StopIteration, AttributeError):
                frame = None
        if frame is None:
            self._input_frame_cache = None
            return
        self._input_frame_cache = (active_page(page), frame)

    def _saved_event_frame(self, page: Any, event: dict[str, Any]) -> Any | None:
        """保存済み iframe 経路を親子順にたどり、対象 frame を復元する。"""
        raw_path = str(event.get('iframe_path', '')).strip()
        if not raw_path:
            return None
        try:
            page = active_page(page)
            cache_key = (page, raw_path)
            cached = self._saved_frame_cache.get(cache_key)
            if cached is not None:
                if cached in page.frames and not cached.is_detached():
                    return cached
                self._saved_frame_cache.pop(cache_key, None)
            selectors = json.loads(raw_path)
            if not isinstance(selectors, list) or not all(isinstance(item, str) for item in selectors):
                return None
            current = page.main_frame
            for selector in selectors:
                matches = []
                for child in current.child_frames:
                    try:
                        if child.frame_element().evaluate(
                            '(element, selector) => element.matches(selector)', selector
                        ):
                            matches.append(child)
                    except Exception:
                        continue
                if len(matches) > 1:
                    matches = [
                        child for child in matches
                        if child.frame_element().is_visible()
                    ]
                if len(matches) != 1:
                    return None
                current = matches[0]
            self._saved_frame_cache[cache_key] = current
            return current
        except Exception:
            return None

    def _event_frame(self, page: Any, event: dict[str, Any]) -> Any | None:
        """指定済みの iframe 経路が解決できない場合は全 frame 検索へ切り替えず失敗させる。"""
        frame = self._saved_event_frame(page, event)
        if str(event.get('iframe_path', '')).strip() and frame is None:
            raise RuntimeError('msg.0592')
        return frame

    def _locator_in_saved_frame(
        self, page: Any, event: dict[str, Any], selector_type: str,
        selector: str, timeout: int, require_actionable: bool=True,
    ) -> Any | None:
        """保存済み frame 内で一意かつ操作可能な要素だけを返す。"""
        frame = self._event_frame(page, event)
        if frame is None:
            return None
        try:
            locator = self._locator(frame, selector_type, selector)
            if locator.count() != 1:
                return None
            match = locator.nth(0)
            if not require_actionable:
                return match
            if not match.is_visible():
                return None
            match.scroll_into_view_if_needed(timeout=max(1, timeout))
            if not is_topmost(match):
                return None
            return match
        except Exception:
            return None

    def _fast_event_locator(
        self, page: Any, event: dict[str, Any], selector: str,
        fallback_selector: str, timeout: int,
    ) -> Any:
        """軽量操作向けに、重複する操作可能性検査を省いて一意な要素を取得する。"""
        selector_type = event['selector_type']
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        if str(event.get('iframe_path', '')).strip():
            frame = self._event_frame(page, event)
            try:
                return self._unique_locator_in_frame(
                    page, frame, selector_type, selector, timeout,
                    require_actionable=False,
                )
            except RuntimeError:
                if fallback_type == 'none' or not fallback_selector:
                    raise
                return self._unique_locator_in_frame(
                    page, frame, fallback_type, fallback_selector, timeout,
                    require_actionable=False,
                )
        locator = self._locator_in_saved_frame(
            page, event, selector_type, selector, timeout,
            require_actionable=False,
        )
        if locator is None and fallback_type != 'none' and fallback_selector:
            locator = self._locator_in_saved_frame(
                page, event, fallback_type, fallback_selector, timeout,
                require_actionable=False,
            )
        if locator is None:
            locator = self._cached_input_locator(page, selector_type, selector, timeout)
        if locator is None and fallback_type != 'none' and fallback_selector:
            locator = self._cached_input_locator(page, fallback_type, fallback_selector, timeout)
        if locator is None:
            locator = self._event_locator(
                page, event, selector, fallback_selector, timeout,
                require_actionable=False,
            )
        self._remember_input_frame(page, locator)
        return locator

    def _execute_event(self, page: Any, event: dict[str, Any], variables: dict[str, str], artifact_dir: Path) -> Any:
        page = active_page(page)
        action = event['action']
        selector = substitute(str(event.get('selector', '')), variables)
        fallback_selector = substitute(str(event.get('fallback_selector', '')), variables)
        value = substitute(str(event.get('value', '')), variables)
        timeout = int(event.get('timeout_ms', 10000))
        page.set_default_timeout(timeout)
        lightweight_action = (
            action in {'fill', 'get_text'}
            or (action == 'press' and not self._requires_spinner_check(event))
        )
        if not lightweight_action:
            # 画面更新を起こし得る操作後は frame キャッシュを破棄する。
            self._input_frame_cache = None
        if action == 'goto':
            page.goto(value, wait_until='domcontentloaded')
        elif action == 'click':
            previous_pages = set(open_pages(page.context))
            self._event_locator(
                page, event, selector, fallback_selector, timeout,
                require_actionable=False,
            ).click()
            settle_new_page(page, previous_pages, timeout)
        elif action == 'fill':
            self._fast_event_locator(
                page, event, selector, fallback_selector, timeout,
            ).fill(value)
        elif action == 'select':
            locator = self._event_locator(
                page, event, selector, fallback_selector, timeout,
                require_actionable=False,
            )
            if value == SELECT_FIRST_VALUE:
                locator.select_option(index=0)
            else:
                locator.select_option(value)
        elif action == 'wait':
            legacy_operable = {'clickable', 'editable', 'selectable'}
            condition = 'operable' if value in legacy_operable else value
            if condition not in {'visible', 'operable', 'hidden'}:
                condition = 'visible'
            if condition == 'hidden':
                self._wait_until_hidden(
                    page, event['selector_type'], selector, timeout, event,
                )
            else:
                self._wait_until_ready(
                    page, event, selector, fallback_selector,
                    condition, timeout,
                )
        elif action == 'wait_hidden':
            self._wait_until_hidden(
                page, event['selector_type'], selector, timeout, event,
            )
        elif action == 'press':
            locator = (
                self._event_locator(
                    page, event, selector, fallback_selector, timeout,
                    require_actionable=False,
                )
                if self._requires_spinner_check(event)
                else self._fast_event_locator(
                    page, event, selector, fallback_selector, timeout,
                )
            )
            locator.press(value)
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
            locator = self._fast_event_locator(
                page, event, selector, fallback_selector, timeout,
            )
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

    def _wait_until_hidden(
        self, page: Any, selector_type: str, selector: str,
        timeout: int=10000, event: dict[str, Any] | None=None,
    ) -> None:
        """画面と配下 frame にある一致要素がすべて非表示または切断されるまで待機する。"""
        deadline = time.monotonic() + timeout / 1000
        while True:
            page = active_page(page)
            frame = self._event_frame(page, event) if event else None
            if frame is not None:
                try:
                    locator = self._locator(frame, selector_type, selector)
                    visible_count = sum(
                        locator.nth(index).is_visible()
                        for index in range(locator.count())
                    )
                except Exception:
                    visible_count = self._visible_locator_count(page, selector_type, selector)
            else:
                # 旧イベントまたは iframe 経路が無効な場合は全 frame を検索する。
                visible_count = self._visible_locator_count(page, selector_type, selector)
            if visible_count == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f'msg.0566{visible_count}msg.0567')
            page.wait_for_timeout(100)

    def _wait_until_ready(
            self, page: Any, event: dict[str, Any], selector: str,
            fallback_selector: str, condition: str, timeout: int,
    ) -> None:
        """複数の一致要素から指定状態を満たす最初の要素が現れるまで待機する。"""
        deadline = time.monotonic() + timeout / 1000
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        while True:
            page = active_page(page)
            frame = self._event_frame(page, event)
            try:
                locators = (
                    [self._locator(frame, event['selector_type'], selector)]
                    if frame is not None
                    else self._locators(page, event['selector_type'], selector)
                )
                matches = [
                    locator.nth(index)
                    for locator in locators
                    for index in range(locator.count())
                ]
                if not matches and fallback_type != 'none' and fallback_selector:
                    fallback_locators = (
                        [self._locator(frame, fallback_type, fallback_selector)]
                        if frame is not None
                        else self._locators(page, fallback_type, fallback_selector)
                    )
                    matches = [
                        locator.nth(index)
                        for locator in fallback_locators
                        for index in range(locator.count())
                    ]
                for match in matches:
                    if self._matches_wait_condition(match, condition, deadline):
                        return
            except Exception as error:
                if not self._is_transient_target_error(error):
                    raise
            if time.monotonic() >= deadline:
                raise RuntimeError(f'msg.0588{condition}')
            page.wait_for_timeout(100)

    @staticmethod
    def _matches_wait_condition(match: Any, condition: str, deadline: float) -> bool:
        """一つの要素が選択された待機状態を満たすか判定する。"""
        if not match.is_visible():
            return False
        if condition == 'visible':
            return True
        if condition == 'operable':
            if not match.is_enabled():
                return False
            # hit-test は表示領域外の要素を判定できないため、マウスを動かさず必要な場合だけ画面内へ移動する。
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                match.scroll_into_view_if_needed(timeout=remaining_ms)
            except Exception:
                return False
            operation = match.evaluate("""element => {
                const tag = element.localName;
                const role = element.getAttribute('role');
                const type = String(element.getAttribute('type') || '').toLowerCase();
                if (tag === 'select' || role === 'combobox') return 'select';
                if (tag === 'textarea' || element.isContentEditable
                    || (tag === 'input' && !['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image'].includes(type))) {
                    return 'input';
                }
                return 'click';
            }""")
            if operation == 'input':
                return bool(match.is_editable() and is_topmost(match))
            if operation == 'select':
                return is_topmost(match)
            # trial click でもマウス移動により hover や focus が発火する画面があるため、
            # 要素への入力を一切行わず hit-test だけで操作可能性を判定する。
            return is_topmost(match)
        return False

    @staticmethod
    def _requires_spinner_check(event: dict[str, Any]) -> bool:
        """画面更新を起こし得る操作だけを Spinner 監視対象にする。"""
        action = str(event.get('action', ''))
        if action in SPINNER_TRIGGER_ACTIONS:
            return True
        if action != 'press':
            return False
        keys = {part.strip().casefold() for part in str(event.get('value', '')).split('+')}
        return bool(keys & {'enter', 'numpadenter', 'tab'})

    def _prepare_spinner_observers(
        self, page: Any, event: dict[str, Any] | None=None,
    ) -> dict[Any, tuple[int, bool]]:
        """各 frame に一度だけ監視器を設置し、操作前の状態を返す。"""
        page = active_page(page)
        try:
            all_frames = page_frames(page)
        except Exception:
            return {}
        current_frames = set(all_frames)
        self._spinner_observed_frames.intersection_update(current_frames)
        frames = all_frames
        if event and str(event.get('iframe_path', '')).strip():
            target_frame = self._event_frame(page, event)
            if target_frame is not None:
                frames = list(dict.fromkeys((page.main_frame, target_frame)))
        states: dict[Any, tuple[int, bool]] = {}
        install_script = """() => {
            const selector = '.slds-spinner, lightning-spinner';
            const visible = () => Array.from(document.querySelectorAll(selector)).some(element => {
                const style = getComputedStyle(element);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) !== 0
                    && element.getClientRects().length > 0;
            });
            if (!window.__webFlowSpinnerWatch) {
                const state = {
                    visible: visible(),
                    revision: 0,
                    startedAt: 0,
                    finishedAt: 0,
                };
                const update = () => {
                    const next = visible();
                    if (next === state.visible) return;
                    state.visible = next;
                    state.revision += 1;
                    if (next) state.startedAt = Date.now();
                    else state.finishedAt = Date.now();
                };
                const observer = new MutationObserver(update);
                observer.observe(document.documentElement, {
                    subtree: true,
                    childList: true,
                    attributes: true,
                    attributeFilter: ['class', 'style', 'hidden'],
                });
                window.__webFlowSpinnerWatch = {state, observer, update};
            }
            window.__webFlowSpinnerWatch.update();
            return {...window.__webFlowSpinnerWatch.state};
        }"""
        read_script = """() => {
            const watch = window.__webFlowSpinnerWatch;
            if (!watch) return null;
            watch.update();
            return {...watch.state};
        }"""
        for frame in frames:
            try:
                already_observed = frame in self._spinner_observed_frames
                state = frame.evaluate(read_script if already_observed else install_script)
                # 同じ Frame オブジェクトでも遷移後は window 上の監視状態が失われる。
                if not state and already_observed:
                    state = frame.evaluate(install_script)
                if not state:
                    self._spinner_observed_frames.discard(frame)
                    continue
                self._spinner_observed_frames.add(frame)
                states[frame] = (int(state.get('revision', 0)), bool(state.get('visible')))
            except Exception:
                self._spinner_observed_frames.discard(frame)
        return states

    def _wait_for_observed_spinner(
        self, page: Any, baseline: dict[Any, tuple[int, bool]],
        timeout: int, prefix: str='', event: dict[str, Any] | None=None,
    ) -> None:
        """短い出現待ちの後、実際に現れた Spinner だけが消えるまで待つ。"""
        started = time.monotonic()
        action = str((event or {}).get('action', ''))
        appearance_ms = 100 if action == 'select' else 150 if action == 'click' else 250
        appearance_deadline = started + appearance_ms / 1000
        timeout_deadline = started + timeout / 1000
        saw_spinner = any(visible for _revision, visible in baseline.values())
        logged = False
        while True:
            states = self._prepare_spinner_observers(page, event)
            visible_count = sum(visible for _revision, visible in states.values())
            changed = any(
                revision > baseline.get(frame, (revision, False))[0]
                for frame, (revision, _visible) in states.items()
            )
            if visible_count:
                saw_spinner = True
                if not logged:
                    self.logger(f'{prefix}msg.0574{visible_count}msg.0567')
                    logged = True
            elif saw_spinner or changed:
                return
            now = time.monotonic()
            if not saw_spinner and now >= appearance_deadline:
                return
            if now >= timeout_deadline:
                raise RuntimeError(f'msg.0566{visible_count}msg.0567')
            active_page(page).wait_for_timeout(50)

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

    def _event_locator(
        self, page: Any, event: dict[str, Any], selector: str,
        fallback_selector: str, timeout: int=10000,
        require_actionable: bool=True,
    ) -> Any:
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        if str(event.get('iframe_path', '')).strip():
            frame = self._event_frame(page, event)
            try:
                return self._unique_locator_in_frame(
                    page, frame, event['selector_type'], selector, timeout,
                    require_actionable,
                )
            except RuntimeError:
                if fallback_type == 'none' or not fallback_selector:
                    raise
                self.logger(f'msg.0212{fallback_type}: "{fallback_selector}"')
                return self._unique_locator_in_frame(
                    page, frame, fallback_type, fallback_selector, timeout,
                    require_actionable,
                )
        saved = self._locator_in_saved_frame(
            page, event, event['selector_type'], selector, timeout,
            require_actionable,
        )
        if saved is not None:
            return saved
        if fallback_type != 'none' and fallback_selector:
            saved_fallback = self._locator_in_saved_frame(
                page, event, fallback_type, fallback_selector, timeout,
                require_actionable,
            )
            if saved_fallback is not None:
                return saved_fallback
        try:
            return self._unique_locator(
                page, event['selector_type'], selector, timeout,
                require_actionable,
            )
        except RuntimeError:
            if fallback_type == 'none' or not fallback_selector:
                raise
            self.logger(f'msg.0212{fallback_type}: "{fallback_selector}"')
            try:
                return self._unique_locator(
                    page, fallback_type, fallback_selector, timeout,
                    require_actionable,
                )
            except Exception as fallback_error:
                raise RuntimeError(f'msg.0565{fallback_error}') from fallback_error

    def _file_input_locator(self, page: Any, event: dict[str, Any], selector: str, fallback_selector: str, timeout: int=10000) -> Any:
        """非表示の場合もある file input を可視性判定なしで一意に取得する。"""
        frame = self._event_frame(page, event)
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        if frame is not None:
            try:
                return self._unique_locator_in_frame(
                    page, frame, event['selector_type'], selector, timeout,
                    require_actionable=False,
                )
            except RuntimeError:
                if fallback_type == 'none' or not fallback_selector:
                    raise
                return self._unique_locator_in_frame(
                    page, frame, fallback_type, fallback_selector, timeout,
                    require_actionable=False,
                )
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
        if fallback_type != 'none' and fallback_selector:
            fallbacks = self._locators(page, fallback_type, fallback_selector)
            fallback_matches = [locator.nth(index) for locator in fallbacks for index in range(locator.count())]
            if len(fallback_matches) == 1:
                return fallback_matches[0]
        raise RuntimeError(f'msg.0423{len(matches)}')
