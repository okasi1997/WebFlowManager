"""Playwright を使用して、登録済みイベントを順番に実行する。"""
from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from core.conditions import decode_guard, evaluate_guard
from i18n import tr
VARIABLE_PATTERN = re.compile('\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}')
HEADLESS_WIDTH = 1920
HEADLESS_HEIGHT = 1080

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

    def run(self, workflow_name: str, events: list[dict[str, Any]], variables: dict[str, str], start_index: int=0, records: list[dict[str, Any]] | None=None, browser_visible: bool=True, storage_state_path: Path | None | bool=False) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError('Playwright is not installed. Run: pip install -r requirements.txt') from error
        artifact_dir = self.project_dir / 'artifacts' / datetime.now().strftime('%Y%m%d_%H%M%S')
        artifact_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.project_dir / 'data' / 'browser_state.json' if storage_state_path is False else storage_state_path
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=not browser_visible, args=self._browser_args(browser_visible))
            context_options = self._context_options(browser_visible)
            if state_path is not None and state_path.exists():
                context_options['storage_state'] = str(state_path)
            context = browser.new_context(**context_options)
            page = context.new_page()
            try:
                execution_records = records or [{'name': tr('msg.0175'), 'data': None}]
                for record_number, record in enumerate(execution_records, 1):
                    self.logger(f'Data [{record_number}/{len(execution_records)}]: {record['name']}')
                    self._execute_workflow_on_page(page, events, variables, artifact_dir, record.get('data'), f'pcl_{record_number}', start_index)
                if state_path is not None:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(state_path))
                self.logger(f'msg.0176{workflow_name}msg.0177')
            finally:
                context.close()
                browser.close()

    def run_batch(self, steps: list[dict[str, Any]], variables: dict[str, str], on_step_start: Callable[[dict[str, Any]], Any] | None=None, on_step_success: Callable[[dict[str, Any], Any], None] | None=None, on_step_failure: Callable[[dict[str, Any], Any, Exception], None] | None=None, on_event_start: Callable[[dict[str, Any], dict[str, Any]], None] | None=None, browser_visible: bool=True, session_name: str='batch', storage_state_path: Path | None | bool=False) -> None:
        """計画済みの全ステップを、一つの browser/context/page で実行する。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError('Playwright is not installed. Run: pip install -r requirements.txt') from error
        safe_session = re.sub('[^A-Za-z0-9_-]', '_', session_name)
        artifact_dir = self.project_dir / 'artifacts' / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + f'_{safe_session}')
        artifact_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.project_dir / 'data' / 'browser_state.json' if storage_state_path is False else storage_state_path
        output_state_path = state_path if session_name in {'batch', 'preamble'} else (state_path.parent / f'{state_path.stem}_{safe_session}.json' if state_path is not None else None)
        # 各並列組は同じログイン状態を読み込むが、終了時の書き込み先は分離する。
        # 複数スレッドによる browser_state.json の同時上書きを避けるためである。
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=not browser_visible, args=self._browser_args(browser_visible))
            options = self._context_options(browser_visible)
            if state_path is not None and state_path.exists():
                options['storage_state'] = str(state_path)
            context = browser.new_context(**options)
            page = context.new_page()
            try:
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
                if output_state_path is not None:
                    output_state_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if output_state_path is not None:
                        context.storage_state(path=str(output_state_path))
                finally:
                    context.close()
                    browser.close()

    @staticmethod
    def _browser_args(browser_visible: bool) -> list[str]:
        if browser_visible:
            return ['--start-maximized']
        return [f'--window-size={HEADLESS_WIDTH},{HEADLESS_HEIGHT}']

    @staticmethod
    def _context_options(browser_visible: bool) -> dict[str, Any]:
        if browser_visible:
            return {'no_viewport': True}
        return {'viewport': {'width': HEADLESS_WIDTH, 'height': HEADLESS_HEIGHT}, 'screen': {'width': HEADLESS_WIDTH, 'height': HEADLESS_HEIGHT}, 'device_scale_factor': 1}

    def _execute_workflow_on_page(self, page: Any, events: list[dict[str, Any]], variables: dict[str, str], artifact_dir: Path, root_data: dict[str, Any] | None, trace: str, start_index: int=0, log_prefix: str='', on_event_start: Callable[[dict[str, Any]], None] | None=None) -> None:
        enabled = [event for event in events if event.get('enabled', 1)][start_index:]
        self._execute_sequence(page, enabled, variables, artifact_dir, root_data, {}, trace, log_prefix, [], on_event_start)

    def _execute_sequence(self, page: Any, events: list[dict[str, Any]], variables: dict[str, str], artifact_dir: Path, root_data: dict[str, Any] | None, loop_context: dict[str, Any], trace: str, log_prefix: str='', loop_progress: list[str] | None=None, on_event_start: Callable[[dict[str, Any]], None] | None=None) -> None:
        # loop/retry は境界イベントを検出し、内側の配列を再帰的に実行する。
        loop_progress = loop_progress or []
        index = 0
        while index < len(events):
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
                    retry_count = int(retry_text) if retry_text else 0
                    if retry_count < 0:
                        raise ValueError
                except ValueError as error:
                    raise ValueError(f"group '{event['name']}' requires a non-negative retry count") from error
                for nested_context, progress, nested_trace in iterations:
                    for attempt in range(1, retry_count + 2):
                        try:
                            self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, nested_context, f'{nested_trace}_retry_{attempt}', log_prefix, progress, on_event_start)
                            break
                        except Exception:
                            if attempt > retry_count:
                                raise
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
                    raise ValueError(f'msg.0178{event['name']}msg.0179')
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
                    self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, nested_context, f'{trace}_{item_number}', log_prefix, progress, on_event_start)
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
                        self._execute_sequence(page, events[index + 1:end], variables, artifact_dir, root_data, loop_context, f'{trace}_retry_{attempt}', log_prefix, loop_progress, on_event_start)
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
            data_path = str(event.get('data_path', ''))
            if data_path and event.get('action') != 'get_text':
                effective['value'] = str(self._resolve_data(root_data, data_path, loop_context))
            prefix = self._event_log_prefix(log_prefix, event, loop_progress)
            self.logger(f'{prefix}msg.0191{event['name']}' + (f' ← {data_path}' if data_path else ''))
            try:
                captured = self._execute_event(page, effective, variables, artifact_dir)
                if action == 'get_text' and data_path:
                    self._assign_data(root_data, data_path, loop_context, captured)
            except Exception as error:
                safe_trace = re.sub('[^A-Za-z0-9_-]', '_', trace)
                screenshot = artifact_dir / f'error_{safe_trace}_{event['id']}.png'
                page.screenshot(path=str(screenshot), full_page=True)
                self.logger(f'msg.0192{error}')
                self.logger(f'msg.0193{screenshot}')
                failure_action = str(event.get('failure_action', 'none'))
                if failure_action == 'refresh':
                    self.logger(f'{prefix}msg.0430')
                    page.reload(wait_until='domcontentloaded')
                elif failure_action == 'goto':
                    target = substitute(str(event.get('failure_target', '')), variables)
                    self.logger(f'{prefix}msg.0431{target}')
                    page.goto(target, wait_until='domcontentloaded')
                if not event.get('continue_on_error', 0):
                    raise
            index += 1

    @staticmethod
    def _step_log_prefix(step: dict[str, Any]) -> str:
        workflow = f'msg.0194{step.get('position', '?')}]'
        if step.get('phase') == 'once':
            return workflow
        group = f'msg.0195{step.get('group', '1')}]'
        return f'{group}[Data {step.get('pcl_index', '?')}/{step.get('pcl_total', '?')}]{workflow}'

    @staticmethod
    def _event_log_prefix(log_prefix: str, event: dict[str, Any], loop_progress: list[str]) -> str:
        loops = ''.join((f'msg.0196{progress}]' for progress in loop_progress))
        return f'{log_prefix}msg.0197{event.get('position', '?')}]{loops}'

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
        raise ValueError(f'msg.0178{events[start]['name']}msg.0198')

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

    def _unique_locator(self, page: Any, selector_type: str, selector: str) -> Any:
        locator = self._locator(page, selector_type, selector)
        locator.first.wait_for(state='attached')
        visible = [locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible()]
        actionable = [item for item in visible if self._is_topmost(item)]
        if len(actionable) != 1:
            raise RuntimeError(f'msg.0204{locator.count()}msg.0205{len(visible)}msg.0206{len(actionable)}msg.0073')
        return actionable[0]

    @staticmethod
    def _is_topmost(locator: Any) -> bool:
        return bool(locator.evaluate('element => {\n            const rect = element.getBoundingClientRect();\n            if (rect.width <= 0 || rect.height <= 0 ||\n                rect.right <= 0 || rect.bottom <= 0 ||\n                rect.left >= window.innerWidth || rect.top >= window.innerHeight) return false;\n            const left = Math.max(0, rect.left), right = Math.min(window.innerWidth, rect.right);\n            const top = Math.max(0, rect.top), bottom = Math.min(window.innerHeight, rect.bottom);\n            const points = [\n                [(left + right) / 2, (top + bottom) / 2],\n                [left + Math.min(3, (right - left) / 2), (top + bottom) / 2],\n                [right - Math.min(3, (right - left) / 2), (top + bottom) / 2],\n                [(left + right) / 2, top + Math.min(3, (bottom - top) / 2)],\n                [(left + right) / 2, bottom - Math.min(3, (bottom - top) / 2)]\n            ];\n            return points.some(([x, y]) => {\n                const hit = document.elementFromPoint(x, y);\n                return hit && (hit === element || element.contains(hit));\n            });\n        }'))

    def _execute_event(self, page: Any, event: dict[str, Any], variables: dict[str, str], artifact_dir: Path) -> Any:
        action = event['action']
        selector = substitute(str(event.get('selector', '')), variables)
        fallback_selector = substitute(str(event.get('fallback_selector', '')), variables)
        value = substitute(str(event.get('value', '')), variables)
        timeout = int(event.get('timeout_ms', 10000))
        page.set_default_timeout(timeout)
        if action == 'goto':
            page.goto(value, wait_until='domcontentloaded')
        elif action == 'click':
            self._event_locator(page, event, selector, fallback_selector).click()
        elif action == 'fill':
            self._event_locator(page, event, selector, fallback_selector).fill(value)
            self.logger(f'msg.0207{value}')
        elif action == 'select':
            self._event_locator(page, event, selector, fallback_selector).select_option(value)
            self.logger(f'msg.0208{value}')
        elif action == 'wait':
            self._event_locator(page, event, selector, fallback_selector)
        elif action == 'press':
            self._event_locator(page, event, selector, fallback_selector).press(value)
            self.logger(f'msg.0209{value}')
        elif action == 'upload_file':
            file_path = Path(value)
            if not file_path.is_absolute():
                file_path = self.project_dir / file_path
            file_path = file_path.resolve()
            if not file_path.is_file():
                raise ValueError(f'msg.0421{file_path}')
            self._file_input_locator(page, event, selector, fallback_selector).set_input_files(str(file_path))
            self.logger(f'msg.0422{file_path}')
        elif action == 'get_text':
            variable_name = value.strip()
            if variable_name and not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', variable_name):
                raise ValueError('msg.0210')
            locator = self._event_locator(page, event, selector, fallback_selector)
            captured = (locator.text_content() or '').strip()
            if variable_name:
                variables[variable_name] = captured
                self.logger(f'msg.0211{variable_name}}}：{captured}')
            return captured
        elif action == 'screenshot':
            filename = value or f'screenshot_{event['id']}.png'
            page.screenshot(path=str(artifact_dir / filename), full_page=True)
        elif action == 'pause':
            page.wait_for_timeout(int(value or timeout))
        else:
            raise ValueError(f'Unsupported action: {action}')

    def _event_locator(self, page: Any, event: dict[str, Any], selector: str, fallback_selector: str) -> Any:
        try:
            return self._unique_locator(page, event['selector_type'], selector)
        except RuntimeError as primary_error:
            fallback_type = str(event.get('fallback_selector_type', 'none'))
            if fallback_type == 'none' or not fallback_selector:
                raise
            self.logger(f'msg.0212{fallback_type}')
            try:
                return self._unique_locator(page, fallback_type, fallback_selector)
            except Exception as fallback_error:
                raise RuntimeError(f'msg.0213{primary_error}msg.0214{fallback_error}') from fallback_error

    def _file_input_locator(self, page: Any, event: dict[str, Any], selector: str, fallback_selector: str) -> Any:
        """非表示の場合もある file input を可視性判定なしで一意に取得する。"""
        locator = self._locator(page, event['selector_type'], selector)
        if locator.count() == 1:
            return locator
        fallback_type = str(event.get('fallback_selector_type', 'none'))
        if fallback_type != 'none' and fallback_selector:
            fallback = self._locator(page, fallback_type, fallback_selector)
            if fallback.count() == 1:
                return fallback
        raise RuntimeError(f'msg.0423{locator.count()}')
