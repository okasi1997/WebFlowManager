"""Playwright を使用して、登録済みイベントを順番に実行する。"""
from __future__ import annotations
import hashlib
import json
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from browser.page_runtime import active_page, browser_args, browser_context_options, close_browser_context, is_topmost, launch_persistent_chrome, open_pages, page_frames, restore_storage_state, settle_new_page
from browser.locators import build_locator, locators_across_frames
from browser.profile_runtime import acquire_profile_lease, persistent_profile_dir, profile_lock_error
from core.conditions import decode_guard, evaluate_guard
from core.data_templates import iter_template_instances
from core.settings import SELECT_FIRST_VALUE
from i18n import tr
VARIABLE_PATTERN = re.compile('\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}')
DATA_REFERENCE_PATTERN = re.compile(r'\$\{data:([^{}]+)\}')
SALESFORCE_SPINNER_SELECTOR = '.slds-spinner, lightning-spinner'
SPINNER_TRIGGER_ACTIONS = {'click', 'select', 'goto', 'upload_file'}
DEFAULT_ACTION_STABLE_MS = 250
CLICK_STABLE_POLL_MS = 50


class ExecutionStopped(RuntimeError):
    """イベント境界で利用者の停止要求を受け付けたことを表す。"""

    def __init__(self) -> None:
        super().__init__('execution.stopped')

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

    def __init__(
        self, project_dir: Path, logger: Callable[[str], None],
        action_stable_ms: int=DEFAULT_ACTION_STABLE_MS,
    ) -> None:
        self.project_dir = project_dir
        self.logger = lambda message: logger(tr(message))
        self._input_frame_cache: tuple[Any, Any] | None = None
        self._saved_frame_cache: dict[tuple[Any, str], Any] = {}
        self._spinner_observed_frames: set[Any] = set()
        self._active_event_prefix = ''
        self._session_log_prefix = '[S1] | '
        self.action_stable_ms = max(0, int(action_stable_ms))

    def run_batch(self, steps: list[dict[str, Any]], variables: dict[str, str], on_step_start: Callable[[dict[str, Any]], Any] | None=None, on_step_success: Callable[[dict[str, Any], Any], None] | None=None, on_step_failure: Callable[[dict[str, Any], Any, Exception], None] | None=None, on_event_start: Callable[[dict[str, Any], dict[str, Any]], None] | None=None, stop_requested: Callable[[], bool] | None=None, browser_visible: bool=True, session_name: str='batch', storage_state_path: Path | None | bool=False) -> None:
        """計画済みの全ステップを、一つの browser/context/page で実行する。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError('Playwright is not installed. Run: pip install -r requirements.txt') from error
        safe_session = re.sub('[^A-Za-z0-9_-]', '_', session_name)
        if steps:
            first_step = steps[0]
            session = first_step.get('session', 1)
            group = '' if first_step.get('phase') == 'once' else f" G{first_step.get('group', '1')}"
            self._session_log_prefix = f'[S{session}{group}] | '
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
            profile_lease = None
            if profile_dir is None:
                temporary_profile = tempfile.TemporaryDirectory(prefix='webflow_chrome_')
                profile_dir = Path(temporary_profile.name)
            try:
                profile_lease = acquire_profile_lease(profile_dir)
                context = launch_persistent_chrome(
                    playwright,
                    profile_dir,
                    visible=browser_visible,
                )
                restore_storage_state(context, state_path)
                pages = context.pages
                page = pages[-1] if pages else context.new_page()
                for step_number, step in enumerate(steps, 1):
                    if stop_requested and stop_requested():
                        raise ExecutionStopped()
                    token = on_step_start(step) if on_step_start else None
                    try:
                        record = step.get('record')
                        root_data = record.get('data') if record else None
                        workflow_guards = step.get('guards') or [step.get('guard')]
                        resolver = lambda path: self._resolve_guard_data(root_data, path, {})
                        # 外側 Group、内側 Group、Flow 自身の条件をすべて満たした場合だけ実行する。
                        if all(
                            evaluate_guard(decode_guard(guard), resolver)
                            for guard in workflow_guards
                        ):
                            def event_started(event, current=step):
                                if stop_requested and stop_requested():
                                    raise ExecutionStopped()
                                if on_event_start:
                                    on_event_start(current, event)
                            self._execute_workflow_on_page(page, step['events'], variables, artifact_dir, root_data, f'step_{step_number}', 0, self._step_log_prefix(step), event_started)
                        else:
                            self.logger(
                                f'{self._step_log_prefix(step)}{tr("condition.guard_skipped")}'
                            )
                        if stop_requested and stop_requested():
                            raise ExecutionStopped()
                    except ExecutionStopped:
                        raise
                    except Exception as error:
                        if on_step_failure:
                            on_step_failure(step, token, error)
                        raise
                    if on_step_success:
                        on_step_success(step, token)
            except Exception as error:
                converted = profile_lock_error(error)
                if converted is not None:
                    raise converted from error
                raise
            finally:
                active_error = sys.exc_info()[1]
                cleanup_error: Exception | None = None
                # 利用者による安全停止は正常な制御なので、エラー調査用のブラウザー待機を行わない。
                if (
                    active_error is not None
                    and not isinstance(active_error, ExecutionStopped)
                    and browser_visible
                    and context is not None
                ):
                    self.logger(
                        f'{self._session_log_prefix}'
                        f'{tr("execution.stopped_close_browser")}'
                    )
                    while True:
                        # エラー確認中の終了操作でも、ブラウザーを閉じて後処理へ進める。
                        if stop_requested and stop_requested():
                            break
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
                if profile_lease is not None:
                    profile_lease.release()
                try:
                    if temporary_profile is not None:
                        temporary_profile.cleanup()
                except Exception as error:
                    cleanup_error = cleanup_error or error
                if cleanup_error is not None:
                    if active_error is None:
                        raise cleanup_error
                    try:
                        self.logger(f'{self._session_log_prefix}Browser cleanup failed: {cleanup_error}')
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
                raise TimeoutError('error.event_group_timeout')
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
                    self.logger(
                        f'{self._event_log_prefix(log_prefix, event, loop_progress)}'
                        f'{tr("condition.guard_skipped")}'
                    )
                    index = end + 1
                    continue
                path = str(event.get('data_path', '')).strip()
                iterations: list[tuple[dict[str, Any], list[str], str]] = [(loop_context, loop_progress, trace)]
                if path:
                    items = self._resolve_data(root_data, path, loop_context)
                    if not isinstance(items, list):
                        raise ValueError(
                            f'{tr("execution.loop_path_prefix")}{path}'
                            f'{tr("execution.not_list_suffix")}'
                        )
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
                            self.logger(
                                f'{group_prefix}{tr("event.retry_attempt_prefix")}'
                                f'{attempt}/{retry_count + 1}'
                                f'{tr("execution.retry_failed_infix")}{tr(str(error))}'
                            )
                            if retry_interval_ms:
                                self.logger(
                                    f'{group_prefix}{tr("execution.retry_wait_prefix")}'
                                    f'{retry_interval_ms} ms'
                                )
                                page.wait_for_timeout(retry_interval_ms)
                index = end + 1
                continue
            if action == 'group_end':
                raise ValueError('group_end has no matching group_start')
            if action == 'loop_start':
                end = self._matching_loop_end(events, index)
                group_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
                if not evaluate_guard(group_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                    self.logger(
                        f'{self._event_log_prefix(log_prefix, event, loop_progress)}'
                        f'{tr("condition.guard_skipped")}'
                    )
                    index = end + 1
                    continue
                path = str(event.get('data_path', ''))
                if not path:
                    raise ValueError(
                        f'{tr("execution.loop_quote_prefix")}{event["name"]}'
                        f'{tr("execution.loop_data_link_missing_suffix")}'
                    )
                items = self._resolve_data(root_data, path, loop_context)
                if not isinstance(items, list):
                    raise ValueError(
                        f'{tr("execution.loop_path_prefix")}{path}'
                        f'{tr("execution.not_list_suffix")}'
                    )
                event_prefix = self._event_log_prefix(log_prefix, event, loop_progress)
                self.logger(
                    f'{event_prefix}{tr("execution.loop_log_infix")}{path}'
                    f'{tr("execution.total_infix")}{len(items)}'
                    f'{tr("execution.iterations_suffix")}'
                )
                for item_number, item in enumerate(items, 1):
                    progress = [*loop_progress, f'{item_number}/{len(items)}']
                    self.logger(
                        f'{self._event_log_prefix(log_prefix, event, progress)}'
                        f'{tr("execution.loop_log_infix")}{path} [{item_number}/{len(items)}]'
                    )
                    nested_context = dict(loop_context)
                    nested_context[path] = item
                    self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, nested_context, f'{trace}_{item_number}', log_prefix, progress, on_event_start, deadline)
                index = end + 1
                continue
            if action == 'loop_end':
                raise ValueError('execution.unmatched_loop_end')
            if action == 'retry_start':
                end = self._matching_retry_end(events, index)
                group_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
                if not evaluate_guard(group_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                    self.logger(
                        f'{self._event_log_prefix(log_prefix, event, loop_progress)}'
                        f'{tr("condition.guard_skipped")}'
                    )
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
                    self.logger(
                        f'{retry_prefix}{tr("execution.retry_scope_attempt_infix")}'
                        f'{attempt}/{total_attempts}]'
                    )
                    try:
                        self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, loop_context, f'{trace}_retry_{attempt}', log_prefix, loop_progress, on_event_start, deadline)
                        break
                    except Exception:
                        if attempt >= total_attempts:
                            self.logger(
                                f'{retry_prefix}{tr("execution.retry_scope_failed_after_infix")}'
                                f'{retry_count}{tr("execution.attempts_failed_suffix")}'
                            )
                            raise
                        self.logger(
                            f'{retry_prefix}{tr("execution.retry_scope_next_attempt_infix")}'
                            f'{attempt + 1}/{total_attempts}]'
                        )
                index = end + 1
                continue
            if action == 'retry_end':
                raise ValueError('retry_end has no matching retry_start')
            event_guard = decode_guard(event.get('guard', event.get('guard_json', '')))
            if not evaluate_guard(event_guard, lambda path: self._resolve_guard_data(root_data, path, loop_context)):
                self.logger(
                    f'{self._event_log_prefix(log_prefix, event, loop_progress)}'
                    f'{tr("condition.guard_skipped")}'
                )
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
            self._active_event_prefix = prefix
            try:
                detail = self._event_log_detail(effective, variables)
            except ValueError:
                detail = action
            self.logger(
                f'{prefix}{tr("execution.execute_infix")}{event["name"]}'
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
                            raise TimeoutError('error.event_group_timeout')
                        break
                    except Exception as attempt_error:
                        if attempt > retry_count:
                            raise
                        self.logger(
                            f'{prefix}{tr("event.retry_attempt_prefix")}'
                            f'{attempt}/{retry_count + 1}'
                            f'{tr("execution.retry_failed_infix")}{tr(str(attempt_error))}'
                        )
                        if retry_interval_ms:
                            self.logger(
                                f'{prefix}{tr("execution.retry_wait_prefix")}{retry_interval_ms} ms'
                            )
                            page.wait_for_timeout(retry_interval_ms)
                if action == 'get_text' and data_path:
                    self._assign_data(root_data, data_path, loop_context, captured)
            except Exception as error:
                try:
                    page = active_page(page)
                    failure_url = str(page.url)
                except Exception:
                    failure_url = ''
                screenshot = artifact_dir / self._failure_screenshot_name(trace, event['id'])
                artifact_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
                self.logger(f'{prefix}{tr("error.failure_prefix")}{tr(str(error))}')
                if failure_url:
                    self.logger(f'{prefix}Failure URL: {failure_url}')
                self.logger(f'{prefix}{tr("execution.screenshot_prefix")}{screenshot}')
                failure_action = str(event.get('failure_action', 'none'))
                if failure_action == 'refresh':
                    self.logger(f'{prefix}{tr("execution.failure_refresh")}')
                    page.reload(wait_until='domcontentloaded')
                elif failure_action == 'goto':
                    target = substitute(str(effective.get('failure_target', '')), variables)
                    self.logger(f'{prefix}{tr("execution.failure_goto_prefix")}{target}')
                    page.goto(target, wait_until='domcontentloaded')
                if not event.get('continue_on_error', 0):
                    raise
            index += 1

    @staticmethod
    def _failure_screenshot_name(trace: str, event_id: Any) -> str:
        """再実行階層で肥大化しない、長さを制限したエラー画像名を返す。"""
        safe_trace = re.sub('[^A-Za-z0-9_-]', '_', str(trace))
        # retry の試行番号は保存先の識別に不要なため、毎回同じ基準名へ戻す。
        stable_trace = re.sub(r'_retry_\d+', '', safe_trace).strip('_') or 'run'
        safe_event_id = re.sub('[^A-Za-z0-9_-]', '_', str(event_id))[:16] or 'event'
        if len(stable_trace) > 64:
            digest = hashlib.sha256(stable_trace.encode('utf-8')).hexdigest()[:10]
            stable_trace = f'{stable_trace[:53]}_{digest}'
        return f'error_{stable_trace}_{safe_event_id}.png'

    @staticmethod
    def _step_log_prefix(step: dict[str, Any]) -> str:
        context = WorkflowExecutor._step_log_context(step)
        return f"{context} F{step.get('position', '?')} | "

    @staticmethod
    def _step_log_context(step: dict[str, Any]) -> str:
        """並列実行でも追跡できる固定長に近い実行コンテキストを返す。"""
        session = step.get('session', 1)
        if step.get('phase') == 'once':
            return f'[S{session}]'
        return (
            f"[S{session} G{step.get('group', '1')} "
            f"D{step.get('pcl_index', '?')}/{step.get('pcl_total', '?')}]"
        )

    @staticmethod
    def _event_log_prefix(log_prefix: str, event: dict[str, Any], loop_progress: list[str]) -> str:
        context = log_prefix.removesuffix(' | ')
        loops = ''.join((f' L{progress}' for progress in loop_progress))
        return f"{context} E{event.get('position', '?')}{loops} | "

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
        raise ValueError(
            f'{tr("execution.loop_quote_prefix")}{events[start]["name"]}'
            f'{tr("execution.missing_loop_end_suffix")}'
        )

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
    def _matching_template_data(value: Any, template_key: str) -> list[dict[str, Any]]:
        """指定したテンプレート ID または名前に一致する実体データを返す。"""
        return [
            instance.get('data', {}) for _path, instance in iter_template_instances(value)
            if template_key in {
                str(instance.get('template_id', '')),
                str(instance.get('template_name', '')).strip(),
            }
        ]

    @classmethod
    def _template_data_in_scope(
        cls,
        root_data: dict[str, Any],
        template_path: str,
        template_key: str,
        loop_context: dict[str, Any],
    ) -> Any:
        """現在のループ要素を優先してテンプレート実体を解決する。"""
        if template_path in loop_context:
            return loop_context[template_path]

        # 内側のループから順に探索し、現在処理中のデータ要素に属する
        # テンプレートを、ルート全体にある同種テンプレートより優先する。
        for context_path in reversed(loop_context):
            current_item = loop_context[context_path]
            matches = cls._matching_template_data(current_item, template_key)
            if matches:
                return matches[0] if len(matches) == 1 else matches

        # ループ外では従来どおり、同じテンプレートの全実体を一覧として返す。
        return cls._matching_template_data(root_data, template_key)

    @staticmethod
    def _resolve_child_data(
        current: Any,
        child_parts: list[str],
        prefix: list[str],
        loop_context: dict[str, Any],
    ) -> Any:
        """辞書階層をたどり、途中のリストは現在のループ要素へ置き換える。"""
        for part in child_parts:
            current_path = '.'.join(prefix)
            if isinstance(current, list):
                if current_path not in loop_context:
                    raise ValueError(
                        f'{tr("execution.field_prefix")}{current_path}'
                        f'{tr("execution.list_requires_loop_suffix")}'
                    )
                current = loop_context[current_path]
            prefix.append(part)
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f'{tr("execution.field_missing_prefix")}{".".join(prefix)}')
            current = current[part]

        current_path = '.'.join(prefix)
        if isinstance(current, list) and current_path in loop_context:
            return loop_context[current_path]
        return current

    @classmethod
    def _resolve_data(cls, root_data: dict[str, Any] | None, path: str, loop_context: dict[str, Any]) -> Any:
        if root_data is None:
            raise ValueError(
                f'{tr("execution.linked_data_prefix")}{path}'
                f'{tr("execution.data_missing_suffix")}'
            )
        parts = path.split('.')
        if len(parts) >= 2 and parts[0] == '@template':
            template_path = '.'.join(parts[:2])
            current = cls._template_data_in_scope(
                root_data, template_path, parts[1], loop_context,
            )
            return cls._resolve_child_data(current, parts[2:], parts[:2], loop_context)
        return cls._resolve_child_data(root_data, parts, [], loop_context)

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

    @classmethod
    def _assign_data(cls, root_data: dict[str, Any] | None, path: str, loop_context: dict[str, Any], value: Any) -> None:
        """取得した値を、現在処理中の PCL または list 要素へ書き戻す。"""
        if root_data is None:
            raise ValueError(
                f'{tr("execution.linked_data_prefix")}{path}'
                f'{tr("execution.data_missing_suffix")}'
            )
        parts = path.split('.')
        if len(parts) >= 3 and parts[0] == '@template':
            template_path = '.'.join(parts[:2])
            current = cls._template_data_in_scope(
                root_data, template_path, parts[1], loop_context,
            )
            if not isinstance(current, dict):
                raise ValueError(
                    f'{tr("execution.field_prefix")}{template_path}'
                    f'{tr("execution.list_requires_loop_suffix")}'
                )
            for index, part in enumerate(parts[2:]):
                current_path = '.'.join(parts[:index + 3])
                if part not in current:
                    raise ValueError(f'{tr("execution.field_missing_prefix")}{current_path}')
                if index == len(parts[2:]) - 1:
                    if isinstance(current[part], (dict, list)):
                        raise ValueError(f'{tr("execution.field_missing_prefix")}{current_path}')
                    current[part] = value
                    return
                current = current[part]
                if isinstance(current, list):
                    if current_path not in loop_context:
                        raise ValueError(
                            f'{tr("execution.field_prefix")}{current_path}'
                            f'{tr("execution.list_requires_loop_suffix")}'
                        )
                    current = loop_context[current_path]
                if not isinstance(current, dict):
                    raise ValueError(f'{tr("execution.field_missing_prefix")}{current_path}')
            return
        current: Any = root_data
        prefix: list[str] = []
        for index, part in enumerate(parts):
            prefix.append(part)
            current_path = '.'.join(prefix)
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f'{tr("execution.field_missing_prefix")}{current_path}')
            if index == len(parts) - 1:
                if isinstance(current[part], (dict, list)):
                    raise ValueError(f'{tr("execution.field_missing_prefix")}{current_path}')
                current[part] = value
                return
            current = current[part]
            if isinstance(current, list):
                if current_path not in loop_context:
                    raise ValueError(
                        f'{tr("execution.field_prefix")}{current_path}'
                        f'{tr("execution.list_requires_loop_suffix")}'
                    )
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
            raise RuntimeError(
                f'{tr("selector.unique_required_prefix")}{len(all_matches)}'
                f'{tr("selector.visible_count_infix")}{len(visible)}'
                f'{tr("selector.actionable_count_infix")}{len(actionable)}'
                f'{tr("common.item_count_suffix")}'
            )
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
                if len(visible) == 1 and not require_actionable:
                    return visible[0]
                if len(visible) == 1:
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
                    f'{tr("selector.unique_required_prefix")}{len(matches)}'
                    f'{tr("selector.visible_count_infix")}{len(visible)}'
                    f'{tr("selector.actionable_count_infix")}{len(actionable)}'
                    f'{tr("common.item_count_suffix")}'
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
                        if not child.is_detached()
                        and not str(getattr(child, 'url', '')).startswith('chrome-error://')
                        and child.frame_element().is_visible()
                    ]
                if len(matches) > 1:
                    sized_matches: list[tuple[float, Any]] = []
                    for child in matches:
                        try:
                            box = child.frame_element().bounding_box()
                            area = float(box['width']) * float(box['height']) if box else 0
                            if area > 4:
                                sized_matches.append((area, child))
                        except (AttributeError, KeyError, TypeError, ValueError):
                            continue
                    if sized_matches:
                        largest = max(area for area, _child in sized_matches)
                        matches = [
                            child for area, child in sized_matches
                            if area == largest
                        ]
                if len(matches) != 1:
                    return None
                current = matches[0]
            self._saved_frame_cache[cache_key] = current
            return current
        except Exception:
            return None

    def _event_frame(
        self, page: Any, event: dict[str, Any], timeout: int=0,
    ) -> Any | None:
        """指定済みの iframe 経路が解決できない場合は全 frame 検索へ切り替えず失敗させる。"""
        raw_path = str(event.get('iframe_path', '')).strip()
        if not raw_path:
            return None
        deadline = time.monotonic() + max(0, timeout) / 1000
        while True:
            frame = self._saved_event_frame(page, event)
            if frame is not None:
                return frame
            if time.monotonic() >= deadline:
                raise RuntimeError('error.iframe_not_unique')
            active_page(page).wait_for_timeout(100)

    def _locator_in_saved_frame(
        self, page: Any, event: dict[str, Any], selector_type: str,
        selector: str, timeout: int, require_actionable: bool=True,
    ) -> Any | None:
        """保存済み frame 内で一意かつ操作可能な要素だけを返す。"""
        frame = self._event_frame(page, event, timeout)
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
            frame = self._event_frame(page, event, timeout)
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

    def _screenshot_event_locator(
        self, page: Any, event: dict[str, Any], selector: str,
        fallback_selector: str, timeout: int,
    ) -> Any:
        """スクリーンショット用に、保存時と同じ frame 内の可視要素を取得する。"""
        page = active_page(page)
        # iframe パスが空の場合はメイン frame を意味する。
        # 全 frame を横断すると html/body などが iframe ごとに重複してしまうため、
        # 要素選択時と同じ探索範囲へ明示的に限定する。
        frame = (
            self._event_frame(page, event, timeout)
            if str(event.get('iframe_path', '')).strip()
            else page.main_frame
        )
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        candidates = [(event['selector_type'], selector)]
        if fallback_type != 'none' and fallback_selector:
            candidates.append((fallback_type, fallback_selector))

        last_error: Exception | None = None
        for index, (selector_type, selector_value) in enumerate(candidates):
            try:
                locator = self._locator(frame, selector_type, selector_value)
                matches = [locator.nth(item) for item in range(locator.count())]
                visible = [match for match in matches if match.is_visible()]
                if len(matches) == 1 and len(visible) == 1:
                    if index:
                        self.logger(
                            f'{self._active_event_prefix}{tr("selector.fallback_used_prefix")}'
                            f'{selector_type}: "{selector_value}"'
                        )
                    return visible[0]
                last_error = RuntimeError(
                    f'{tr("selector.unique_required_prefix")}{len(matches)}'
                    f'{tr("selector.visible_count_infix")}{len(visible)}'
                    f'{tr("selector.actionable_count_infix")}0'
                    f'{tr("common.item_count_suffix")}'
                )
            except Exception as error:
                last_error = error

        if len(candidates) > 1 and last_error is not None:
            raise RuntimeError(
                f'{tr("error.fallback_selector_failed_prefix")}{tr(str(last_error))}'
            ) from last_error
        if last_error is not None:
            raise last_error
        raise RuntimeError(tr('selector.unique_required_prefix'))

    @staticmethod
    def _click_target_snapshot(locator: Any) -> tuple[str, float, float, float, float]:
        snapshot = locator.evaluate("""element => {
            if (!element.__wfmStableTargetId) {
                element.__wfmStableTargetId = `${Date.now()}-${Math.random()}`;
            }
            const rect = element.getBoundingClientRect();
            return [element.__wfmStableTargetId, rect.x, rect.y, rect.width, rect.height];
        }""")
        return tuple(snapshot)

    def _wait_for_stable_action_target(self, locator: Any, timeout: int) -> None:
        """操作前に、同じ有効かつ非遮蔽の要素が短時間静止するまで待機する。"""
        deadline = time.monotonic() + timeout / 1000
        stable_since: float | None = None
        previous: tuple[str, float, float, float, float] | None = None
        while time.monotonic() < deadline:
            try:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                locator.scroll_into_view_if_needed(timeout=remaining_ms)
                ready = locator.is_visible() and locator.is_enabled() and is_topmost(locator)
                current = self._click_target_snapshot(locator) if ready else None
            except Exception as error:
                if not self._is_transient_target_error(error):
                    raise
                current = None
            now = time.monotonic()
            if current is not None and self.action_stable_ms == 0:
                return
            if current is not None and current == previous:
                stable_since = stable_since if stable_since is not None else now
                if (now - stable_since) * 1000 >= self.action_stable_ms:
                    return
            else:
                previous = current
                stable_since = now if current is not None else None
            locator.page.wait_for_timeout(CLICK_STABLE_POLL_MS)
        raise RuntimeError('Action target did not remain actionable and stable')

    @staticmethod
    def _arm_click_receipt(locator: Any) -> tuple[str, Any]:
        token = f'{time.time_ns()}'
        handle = locator.element_handle()
        if handle is None:
            raise RuntimeError('Click target was detached before the click')
        handle.evaluate("""(element, token) => {
            element.__wfmClickReceipt = '';
            const receive = event => {
                if (!event.composedPath().includes(element)) return;
                element.__wfmClickReceipt = token;
                element.ownerDocument.removeEventListener('click', receive, true);
            };
            element.ownerDocument.addEventListener('click', receive, true);
        }""", token)
        return token, handle

    @staticmethod
    def _click_was_received(handle: Any, token: str) -> bool:
        try:
            return bool(handle.evaluate(
                '(element, token) => element.__wfmClickReceipt === token', token,
            ))
        except Exception as error:
            message = str(error).casefold()
            if any(part in message for part in (
                'execution context was destroyed', 'cannot find context with specified id',
                'jshandle is disposed', 'not attached', 'target page has been closed',
            )):
                return True
            raise

    def _wait_for_event_success(
        self, page: Any, event: dict[str, Any], variables: dict[str, str], timeout: int,
    ) -> None:
        raw_value = str(event.get('success_json', '') or '')
        if not raw_value and event.get('action') == 'click':
            # DB 移行前の click を直接実行する場合だけ旧 value を読む。
            raw_value = str(event.get('value', '') or '')
        if not raw_value.strip():
            return
        try:
            config = json.loads(raw_value)
        except (TypeError, ValueError):
            return  # 旧クリックイベントには未使用の自由入力値が残っている場合がある。
        if not isinstance(config, dict):
            return
        condition = str(config.get('condition', 'none'))
        target = substitute(str(config.get('target', '')), variables)
        if condition == 'none':
            return
        if condition == 'url_contains':
            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                if target in active_page(page).url:
                    return
                active_page(page).wait_for_timeout(100)
            raise RuntimeError(f'Event success URL was not observed: {target}')
        if condition not in {'visible', 'hidden', 'operable'} or not target:
            raise RuntimeError('Invalid event success condition')
        success_event = {
            'selector_type': str(config.get('selector_type', 'css')),
            'fallback_selector_type': 'none',
            'iframe_path': str(config.get('iframe_path', '')),
        }
        if condition == 'hidden':
            self._wait_until_hidden(
                page, success_event['selector_type'], target, timeout, success_event,
            )
        else:
            self._wait_until_ready(page, success_event, target, '', condition, timeout)

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
            self._wait_for_event_success(page, event, variables, timeout)
        elif action == 'click':
            previous_pages = set(open_pages(page.context))
            locator = self._event_locator(
                page, event, selector, fallback_selector, timeout,
                require_actionable=False,
            )
            self._wait_for_stable_action_target(locator, timeout)
            click_token, click_handle = self._arm_click_receipt(locator)
            locator.click()
            if not self._click_was_received(click_handle, click_token):
                raise RuntimeError('Click event was not received by the target element')
            settle_new_page(page, previous_pages, timeout)
            self._wait_for_event_success(page, event, variables, timeout)
        elif action == 'fill':
            locator = self._fast_event_locator(
                page, event, selector, fallback_selector, timeout,
            )
            self._wait_for_stable_action_target(locator, timeout)
            locator.fill(value)
        elif action == 'select':
            locator = self._event_locator(
                page, event, selector, fallback_selector, timeout,
                require_actionable=False,
            )
            # 選択肢を変更する直前にも、ドロップダウンの移動やアニメーション終了を確認する。
            self._wait_for_stable_action_target(locator, timeout)
            if value == SELECT_FIRST_VALUE:
                locator.select_option(index=0)
            else:
                locator.select_option(value)
            self._wait_for_event_success(page, event, variables, timeout)
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
            self._wait_for_stable_action_target(locator, timeout)
            locator.press(value)
            self._wait_for_event_success(page, event, variables, timeout)
        elif action == 'upload_file':
            file_path = Path(value)
            if not file_path.is_absolute():
                file_path = self.project_dir / file_path
            file_path = file_path.resolve()
            if not file_path.is_file():
                raise ValueError(f'{tr("error.upload_file_not_found_prefix")}{file_path}')
            self._file_input_locator(page, event, selector, fallback_selector, timeout).set_input_files(str(file_path))
        elif action == 'get_text':
            variable_name = value.strip()
            if variable_name and not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', variable_name):
                raise ValueError('error.get_text_destination_required')
            locator = self._fast_event_locator(
                page, event, selector, fallback_selector, timeout,
            )
            captured = (locator.text_content() or '').strip()
            if variable_name:
                variables[variable_name] = captured
            return captured
        elif action == 'screenshot':
            filename = self._screenshot_filename(event, value)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = artifact_dir / filename
            if selector and event.get('selector_type') != 'none':
                locator = self._screenshot_event_locator(
                    page, event, selector, fallback_selector, timeout,
                )
                scroll_locator = locator
                try:
                    scroll_data = json.loads(str(event.get('scroll_json', ''))) if event.get('scroll_json') else {}
                except json.JSONDecodeError:
                    scroll_data = {}
                scroll_config = scroll_data.get('scroll', {}) if isinstance(scroll_data, dict) else {}
                # 旧形式の scroll_json もそのまま読み込めるようにする。
                if isinstance(scroll_data, dict) and scroll_data.get('selector'):
                    scroll_config = scroll_data
                if isinstance(scroll_config, dict) and scroll_config.get('selector'):
                    scroll_event = {
                        'selector_type': scroll_config.get('selector_type', 'css'),
                        'fallback_selector_type': scroll_config.get('fallback_selector_type', 'none'),
                        'fallback_selector': scroll_config.get('fallback_selector', ''),
                        'iframe_path': scroll_config.get('iframe_path', ''),
                    }
                    scroll_locator = self._screenshot_event_locator(
                        page, scroll_event, str(scroll_config['selector']),
                        str(scroll_config.get('fallback_selector', '')), timeout,
                    )
                self._screenshot_scroll_area(
                    scroll_locator, screenshot_path, timeout,
                    capture_area=locator if scroll_locator is not locator else None,
                )
            else:
                # 対象未指定の既存イベントは、従来どおり主画面全体を保存する。
                page.screenshot(path=str(screenshot_path), full_page=True)
        elif action == 'pause':
            page.wait_for_timeout(int(value or timeout))
        else:
            raise ValueError(f'Unsupported action: {action}')

    @staticmethod
    def _screenshot_filename(
        event: dict[str, Any], value: str, now: datetime | None=None,
    ) -> str:
        """保存前のイベントにも衝突しにくい既定スクリーンショット名を返す。"""
        if value:
            return value
        event_id = event.get('id')
        if event_id is not None:
            return f'screenshot_{event_id}.png'
        # 新規イベントの試行時はまだ DB の ID がないため、マイクロ秒まで含める。
        current = now or datetime.now()
        return f'screenshot_{current:%Y%m%d_%H%M%S_%f}.png'

    @staticmethod
    def _screenshot_scroll_area(
        locator: Any, path: Path, timeout: int,
        capture_area: Any | None=None,
    ) -> None:
        """元の位置を保存し、原点で測定してからスクロールキャプチャーを行う。"""
        locator.scroll_into_view_if_needed(timeout=timeout)
        handle = locator.element_handle(timeout=timeout)
        if handle is None:
            raise RuntimeError('Screenshot target was not found')
        original = handle.evaluate("""element => ({
            x: element.scrollLeft,
            y: element.scrollTop,
            behavior: element.style.getPropertyValue('scroll-behavior'),
            behaviorPriority: element.style.getPropertyPriority('scroll-behavior'),
        })""")
        try:
            handle.evaluate("""element => {
                element.style.setProperty('scroll-behavior', 'auto', 'important');
                element.scrollLeft = 0;
                element.scrollTop = 0;
            }""")
            handle.evaluate(
                "element => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
            )
            WorkflowExecutor._screenshot_scroll_area_from_origin(
                locator, handle, path, timeout, capture_area,
            )
        finally:
            # 測定やキャプチャーが失敗しても、PCL 実行前のスクロール位置と CSS を復元する。
            handle.evaluate("""(element, state) => {
                element.scrollLeft = state.x;
                element.scrollTop = state.y;
                if (state.behavior) {
                    element.style.setProperty(
                        'scroll-behavior', state.behavior, state.behaviorPriority,
                    );
                } else {
                    element.style.removeProperty('scroll-behavior');
                }
            }""", original)

    @staticmethod
    def _screenshot_scroll_area_from_origin(
        locator: Any, handle: Any, path: Path, timeout: int,
        capture_area: Any | None=None,
    ) -> None:
        """原点を基準に範囲を測定し、実際のスクロール領域を分割キャプチャーする。"""
        # 新規イベントは選択時に確定したスクロール要素を直接渡す。
        # 旧イベントでは従来どおりキャプチャー対象自身を使用し、曖昧な祖先推測は行わない。
        metrics = handle.evaluate("""element => {
            return {
                clientWidth: element.clientWidth,
                clientHeight: element.clientHeight,
                scrollWidth: element.scrollWidth,
                scrollHeight: element.scrollHeight,
                scrollLeft: element.scrollLeft,
                scrollTop: element.scrollTop,
                clientLeft: element.clientLeft,
                clientTop: element.clientTop,
                borderLeftWidth: parseFloat(getComputedStyle(element).borderLeftWidth) || 0,
                borderTopWidth: parseFloat(getComputedStyle(element).borderTopWidth) || 0,
                borderRightWidth: parseFloat(getComputedStyle(element).borderRightWidth) || 0,
                borderBottomWidth: parseFloat(getComputedStyle(element).borderBottomWidth) || 0,
                borderLeftColor: getComputedStyle(element).borderLeftColor,
                borderTopColor: getComputedStyle(element).borderTopColor,
                borderRightColor: getComputedStyle(element).borderRightColor,
                borderBottomColor: getComputedStyle(element).borderBottomColor,
            };
        }""")
        box = handle.bounding_box()
        if box is None or metrics['clientWidth'] < 1 or metrics['clientHeight'] < 1:
            raise RuntimeError('Screenshot target is not visible')
        width, height = int(metrics['scrollWidth']), int(metrics['scrollHeight'])
        view_width, view_height = int(metrics['clientWidth']), int(metrics['clientHeight'])
        crop = (0, 0, width, height)
        capture_handle = None
        capture_contains_scroll = False
        shell_box = None
        shell_image = None
        shell_content_rect = None
        if capture_area is not None:
            capture_handle = capture_area.element_handle(timeout=timeout)
            if capture_handle is None:
                raise RuntimeError('Screenshot boundary was not found')
            area_box = capture_area.bounding_box()
            if area_box is None:
                raise RuntimeError('Screenshot boundary is not visible')
            content_x = box['x'] + metrics['clientLeft']
            content_y = box['y'] + metrics['clientTop']
            try:
                capture_contains_scroll = bool(capture_handle.evaluate(
                    '(boundary, scrolling) => boundary !== scrolling && boundary.contains(scrolling)',
                    handle,
                ))
            except Exception:
                # frame が異なる場合は DOM の包含判定ができないため、従来の範囲裁切を使う。
                capture_contains_scroll = False
            if capture_contains_scroll:
                shell_box = area_box
                shell_content_rect = {
                    'x': content_x - area_box['x'],
                    'y': content_y - area_box['y'],
                    'width': view_width,
                    'height': view_height,
                }
            else:
                left = round(area_box['x'] - content_x + metrics['scrollLeft'])
                top = round(area_box['y'] - content_y + metrics['scrollTop'])
                right = round(
                    area_box['x'] + area_box['width']
                    - content_x + metrics['scrollLeft']
                )
                bottom = round(
                    area_box['y'] + area_box['height']
                    - content_y + metrics['scrollTop']
                )
                left, right = sorted((left, right))
                top, bottom = sorted((top, bottom))
                crop = (
                    max(0, min(left, width - 1)), max(0, min(top, height - 1)),
                    max(1, min(right, width)), max(1, min(bottom, height)),
                )
        # Qt と Chromium の画像上限を越えて不安定になる前に明示的に停止する。
        if width > 32767 or height > 32767 or width * height > 100_000_000:
            raise RuntimeError(f'Screenshot area is too large: {width}x{height}')

        def positions(total: int, viewport: int) -> list[int]:
            last = max(0, total - viewport)
            return list(dict.fromkeys([*range(0, last + 1, viewport), last]))

        canvas = None
        painter = None
        ancestor_frames: list[tuple[Any, dict[str, Any]]] = []
        try:
            if capture_handle is not None:
                # iframe の外側にある Salesforce などの固定ヘッダーもタイルへ写り込む。
                # キャプチャー対象 frame の祖先だけを処理し、iframe 内の選択範囲には触れない。
                owner_frame = capture_handle.owner_frame()
                parent_frame = owner_frame.parent_frame if owner_frame is not None else None
                while parent_frame is not None:
                    state = parent_frame.evaluate("""() => {
                        const scrolling = document.scrollingElement || document.documentElement;
                        const hidden = [];
                        for (const element of document.querySelectorAll('*')) {
                            const position = getComputedStyle(element).position;
                            if (position !== 'fixed' && position !== 'sticky') continue;
                            const rect = element.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            hidden.push({
                                element,
                                value: element.style.getPropertyValue('visibility'),
                                priority: element.style.getPropertyPriority('visibility'),
                            });
                            element.style.setProperty('visibility', 'hidden', 'important');
                        }
                        window.__wfmScreenshotParentOverlays = hidden;
                        return {
                            x: scrolling.scrollLeft,
                            y: scrolling.scrollTop,
                            behavior: scrolling.style.getPropertyValue('scroll-behavior'),
                            behaviorPriority: scrolling.style.getPropertyPriority('scroll-behavior'),
                        };
                    }""")
                    # 後続処理で例外が発生しても復元対象から漏れないよう、先に記録する。
                    ancestor_frames.append((parent_frame, state))
                    parent_frame.evaluate("""() => {
                        const scrolling = document.scrollingElement || document.documentElement;
                        scrolling.style.setProperty('scroll-behavior', 'auto', 'important');
                    }""")
                    parent_frame = parent_frame.parent_frame
                capture_handle.evaluate("""boundary => {
                    const hidden = [];
                    for (const element of boundary.ownerDocument.querySelectorAll('*')) {
                        if (element === boundary || boundary.contains(element) || element.contains(boundary)) {
                            continue;
                        }
                        const position = getComputedStyle(element).position;
                        if (position !== 'fixed' && position !== 'sticky') {
                            continue;
                        }
                        const rect = element.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) {
                            continue;
                        }
                        hidden.push({
                            element,
                            value: element.style.getPropertyValue('visibility'),
                            priority: element.style.getPropertyPriority('visibility'),
                        });
                        element.style.setProperty('visibility', 'hidden', 'important');
                    }
                    // finally で確実に元へ戻すため、対象要素側へ一時的に保持する。
                    boundary.__wfmScreenshotHiddenOverlays = hidden;
                }""")
                if capture_contains_scroll and shell_box is not None:
                    shell_image = QImage.fromData(locator.page.screenshot(
                        clip=shell_box, animations='disabled', timeout=timeout,
                    ), 'PNG')
                    if shell_image.isNull():
                        raise RuntimeError('Screenshot boundary could not be decoded')
            for y in positions(height, view_height):
                for x in positions(width, view_width):
                    for parent_frame, state in ancestor_frames:
                        # ページ側スクリプトの影響を受けても、iframe の画面位置を固定する。
                        parent_frame.evaluate("""state => {
                            const scrolling = document.scrollingElement || document.documentElement;
                            scrolling.scrollLeft = state.x;
                            scrolling.scrollTop = state.y;
                        }""", state)
                    actual = handle.evaluate("""(element, point) => {
                        element.scrollLeft = point.x;
                        element.scrollTop = point.y;
                        return {x: element.scrollLeft, y: element.scrollTop};
                    }""", {'x': x, 'y': y})
                    # スクロール反映と遅延描画に一フレームだけ待機時間を与える。
                    handle.evaluate("element => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                    image = QImage.fromData(locator.page.screenshot(
                        clip={
                            'x': box['x'] + metrics['clientLeft'],
                            'y': box['y'] + metrics['clientTop'],
                            'width': view_width,
                            'height': view_height,
                        },
                        animations='disabled', timeout=timeout,
                    ), 'PNG')
                    if image.isNull():
                        raise RuntimeError('Screenshot tile could not be decoded')
                    if canvas is None:
                        scale_x = image.width() / view_width
                        scale_y = image.height() / view_height
                        canvas = QImage(
                            round(width * scale_x), round(height * scale_y),
                            QImage.Format.Format_ARGB32,
                        )
                        canvas.fill(QColor('white'))
                        painter = QPainter(canvas)
                    target_x = round(actual['x'] * scale_x)
                    target_y = round(actual['y'] * scale_y)
                    copy_width = min(image.width(), canvas.width() - target_x)
                    copy_height = min(image.height(), canvas.height() - target_y)
                    painter.drawImage(
                        QRectF(target_x, target_y, copy_width, copy_height), image,
                        QRectF(0, 0, copy_width, copy_height),
                    )
        finally:
            if painter is not None:
                painter.end()
            try:
                if capture_handle is not None:
                    capture_handle.evaluate("""boundary => {
                        for (const item of boundary.__wfmScreenshotHiddenOverlays || []) {
                            if (item.value) {
                                item.element.style.setProperty('visibility', item.value, item.priority);
                            } else {
                                item.element.style.removeProperty('visibility');
                            }
                        }
                        delete boundary.__wfmScreenshotHiddenOverlays;
                    }""")
            finally:
                for parent_frame, state in reversed(ancestor_frames):
                    parent_frame.evaluate("""state => {
                            for (const item of window.__wfmScreenshotParentOverlays || []) {
                                if (item.value) {
                                    item.element.style.setProperty('visibility', item.value, item.priority);
                                } else {
                                    item.element.style.removeProperty('visibility');
                                }
                            }
                            delete window.__wfmScreenshotParentOverlays;
                            const scrolling = document.scrollingElement || document.documentElement;
                            scrolling.scrollLeft = state.x;
                            scrolling.scrollTop = state.y;
                            if (state.behavior) {
                                scrolling.style.setProperty(
                                    'scroll-behavior', state.behavior, state.behaviorPriority,
                                );
                            } else {
                                scrolling.style.removeProperty('scroll-behavior');
                            }
                    }""", state)
        if canvas is not None and crop != (0, 0, width, height):
            left, top, right, bottom = crop
            canvas = canvas.copy(
                round(left * scale_x), round(top * scale_y),
                round((right - left) * scale_x), round((bottom - top) * scale_y),
            )
        if canvas is not None and shell_image is not None and shell_box is not None:
            canvas = WorkflowExecutor._compose_screenshot_shell(
                shell_image, canvas, shell_box, shell_content_rect,
            )
        if canvas is not None and capture_area is None:
            # 分割画像は内側だけをキャプチャーし、最後に選択要素自身の外枠を一度だけ付ける。
            border_left = round(float(metrics.get('borderLeftWidth', 0)) * scale_x)
            border_top = round(float(metrics.get('borderTopWidth', 0)) * scale_y)
            border_right = round(float(metrics.get('borderRightWidth', 0)) * scale_x)
            border_bottom = round(float(metrics.get('borderBottomWidth', 0)) * scale_y)
            if any((border_left, border_top, border_right, border_bottom)):
                framed = QImage(
                    canvas.width() + border_left + border_right,
                    canvas.height() + border_top + border_bottom,
                    QImage.Format.Format_ARGB32,
                )
                framed.fill(QColor('white'))
                frame_painter = QPainter(framed)
                frame_painter.drawImage(
                    QRectF(border_left, border_top, canvas.width(), canvas.height()),
                    canvas,
                    QRectF(0, 0, canvas.width(), canvas.height()),
                )
                if border_top:
                    frame_painter.fillRect(
                        QRectF(0, 0, framed.width(), border_top),
                        QColor(str(metrics.get('borderTopColor', 'transparent'))),
                    )
                if border_bottom:
                    frame_painter.fillRect(
                        QRectF(0, framed.height() - border_bottom, framed.width(), border_bottom),
                        QColor(str(metrics.get('borderBottomColor', 'transparent'))),
                    )
                if border_left:
                    frame_painter.fillRect(
                        QRectF(0, border_top, border_left, canvas.height()),
                        QColor(str(metrics.get('borderLeftColor', 'transparent'))),
                    )
                if border_right:
                    frame_painter.fillRect(
                        QRectF(framed.width() - border_right, border_top, border_right, canvas.height()),
                        QColor(str(metrics.get('borderRightColor', 'transparent'))),
                    )
                frame_painter.end()
                canvas = framed
        if canvas is None or not canvas.save(str(path), 'PNG'):
            raise RuntimeError(f'Screenshot could not be saved: {path}')

    @staticmethod
    def _compose_screenshot_shell(
        shell: QImage, content: QImage, shell_box: dict[str, float],
        content_rect: dict[str, float] | None,
    ) -> QImage:
        """外枠を九分割し、中央だけを展開済みスクロール画像へ置き換える。"""
        if content_rect is None or shell_box['width'] <= 0 or shell_box['height'] <= 0:
            return content
        scale_x = shell.width() / shell_box['width']
        scale_y = shell.height() / shell_box['height']
        left = max(0, min(shell.width(), round(content_rect['x'] * scale_x)))
        top = max(0, min(shell.height(), round(content_rect['y'] * scale_y)))
        viewport_width = max(0, round(content_rect['width'] * scale_x))
        viewport_height = max(0, round(content_rect['height'] * scale_y))
        right = max(0, shell.width() - min(shell.width(), left + viewport_width))
        bottom = max(0, shell.height() - min(shell.height(), top + viewport_height))
        result = QImage(
            left + content.width() + right,
            top + content.height() + bottom,
            QImage.Format.Format_ARGB32,
        )
        result.fill(QColor('white'))
        painter = QPainter(result)

        # 外枠の角はそのまま、辺は展開方向だけへ伸ばして中央内容を囲む。
        source_x = (0, left, shell.width() - right)
        source_y = (0, top, shell.height() - bottom)
        source_w = (left, max(0, shell.width() - left - right), right)
        source_h = (top, max(0, shell.height() - top - bottom), bottom)
        target_x = (0, left, left + content.width())
        target_y = (0, top, top + content.height())
        target_w = (left, content.width(), right)
        target_h = (top, content.height(), bottom)
        for row in range(3):
            for column in range(3):
                if row == 1 and column == 1:
                    continue
                if source_w[column] <= 0 or source_h[row] <= 0:
                    continue
                painter.drawImage(
                    QRectF(target_x[column], target_y[row], target_w[column], target_h[row]),
                    shell,
                    QRectF(source_x[column], source_y[row], source_w[column], source_h[row]),
                )
        painter.drawImage(
            QRectF(left, top, content.width(), content.height()), content,
            QRectF(0, 0, content.width(), content.height()),
        )
        painter.end()
        return result

    def _wait_until_hidden(
        self, page: Any, selector_type: str, selector: str,
        timeout: int=10000, event: dict[str, Any] | None=None,
    ) -> None:
        """画面と配下 frame にある一致要素がすべて非表示または切断されるまで待機する。"""
        deadline = time.monotonic() + timeout / 1000
        while True:
            page = active_page(page)
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            frame = self._event_frame(page, event, remaining) if event else None
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
                raise RuntimeError(
                    f'{tr("selector.hidden_wait_timeout_prefix")}{visible_count}'
                    f'{tr("common.item_count_suffix")}'
                )
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
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            frame = self._event_frame(page, event, remaining)
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
                raise RuntimeError(f'{tr("error.wait_condition_not_met_prefix")}{condition}')
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
            target_frame = self._event_frame(
                page, event, int(event.get('timeout_ms', 10000)),
            )
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
                    self.logger(
                        f'{prefix}{tr("selector.salesforce_loading_prefix")}{visible_count}'
                        f'{tr("common.item_count_suffix")}'
                    )
                    logged = True
            elif saw_spinner or changed:
                return
            now = time.monotonic()
            if not saw_spinner and now >= appearance_deadline:
                return
            if now >= timeout_deadline:
                raise RuntimeError(
                    f'{tr("selector.hidden_wait_timeout_prefix")}{visible_count}'
                    f'{tr("common.item_count_suffix")}'
                )
            active_page(page).wait_for_timeout(50)

    def _wait_for_salesforce_spinner_if_present(self, page: Any, timeout: int, prefix: str='') -> None:
        """Spinner がない画面を遅延させず、Salesforce 共通の読込完了待ちを行う。"""
        page = active_page(page)
        visible_count = self._visible_locator_count(page, 'css', SALESFORCE_SPINNER_SELECTOR)
        if visible_count == 0:
            return
        self.logger(
            f'{prefix}{tr("selector.salesforce_loading_prefix")}{visible_count}'
            f'{tr("common.item_count_suffix")}'
        )
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
            frame = self._event_frame(page, event, timeout)
            try:
                return self._unique_locator_in_frame(
                    page, frame, event['selector_type'], selector, timeout,
                    require_actionable,
                )
            except RuntimeError:
                if fallback_type == 'none' or not fallback_selector:
                    raise
                self.logger(
                    f'{self._active_event_prefix}{tr("selector.fallback_used_prefix")}'
                    f'{fallback_type}: "{fallback_selector}"'
                )
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
            self.logger(
                f'{self._active_event_prefix}{tr("selector.fallback_used_prefix")}'
                f'{fallback_type}: "{fallback_selector}"'
            )
            try:
                return self._unique_locator(
                    page, fallback_type, fallback_selector, timeout,
                    require_actionable,
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    f'{tr("error.fallback_selector_failed_prefix")}{tr(str(fallback_error))}'
                ) from fallback_error

    def _file_input_locator(self, page: Any, event: dict[str, Any], selector: str, fallback_selector: str, timeout: int=10000) -> Any:
        """非表示の場合もある file input を可視性判定なしで一意に取得する。"""
        frame = self._event_frame(page, event, timeout)
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
        raise RuntimeError(f'{tr("error.upload_target_not_unique_prefix")}{len(matches)}')
