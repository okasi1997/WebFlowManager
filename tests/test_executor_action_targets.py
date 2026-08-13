import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.executor import WorkflowExecutor


class _Page:
    def wait_for_timeout(self, _timeout: int) -> None:
        pass


class _Locator:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.page = _Page()

    def scroll_into_view_if_needed(self, timeout: int) -> None:
        self.calls.append('scroll')

    def is_visible(self) -> bool:
        self.calls.append('visible')
        return True

    def is_enabled(self) -> bool:
        self.calls.append('enabled')
        return True


class ExecutorActionTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.executor = WorkflowExecutor(Path(self.temporary_dir.name), lambda _message: None)

    def tearDown(self) -> None:
        self.temporary_dir.cleanup()

    def test_action_target_is_scrolled_before_actionability_checks(self) -> None:
        locator = _Locator()

        with (
            patch('core.executor.is_topmost', side_effect=lambda item: item.calls.append('topmost') or True),
            patch.object(self.executor, '_click_target_snapshot', return_value=('target', 0, 0, 10, 10)),
        ):
            self.executor._wait_for_stable_action_target(locator, 1000)

        self.assertEqual(locator.calls[:4], ['scroll', 'visible', 'enabled', 'topmost'])

    def test_zero_stable_time_returns_after_first_actionability_check(self) -> None:
        locator = _Locator()
        executor = WorkflowExecutor(Path(self.temporary_dir.name), lambda _message: None, 0)

        with (
            patch('core.executor.is_topmost', return_value=True),
            patch.object(executor, '_click_target_snapshot', return_value=('target', 0, 0, 10, 10)),
        ):
            executor._wait_for_stable_action_target(locator, 1000)

        self.assertEqual(locator.calls.count('scroll'), 1)

    def test_fill_and_press_use_the_common_pre_action_wait(self) -> None:
        page = Mock()
        page.is_closed.return_value = False
        page.context = Mock()
        locator = Mock()
        base_event = {
            'selector_type': 'css', 'selector': '#target',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'timeout_ms': 1000, 'success_json': '',
        }

        with (
            patch('core.executor.active_page', return_value=page),
            patch.object(self.executor, '_fast_event_locator', return_value=locator),
            patch.object(self.executor, '_event_locator', return_value=locator),
            patch.object(self.executor, '_wait_for_stable_action_target') as wait_for_target,
            patch.object(self.executor, '_wait_for_event_success'),
        ):
            self.executor._execute_event(
                page, {**base_event, 'action': 'fill', 'value': 'text'}, {}, Path(self.temporary_dir.name),
            )
            self.executor._execute_event(
                page, {**base_event, 'action': 'press', 'value': 'Enter'}, {}, Path(self.temporary_dir.name),
            )

        self.assertEqual(wait_for_target.call_count, 2)
        locator.fill.assert_called_once_with('text')
        locator.press.assert_called_once_with('Enter')

    def test_scroll_area_screenshot_restores_element_state(self) -> None:
        """全景撮影後は対象要素のスタイルとスクロール位置を必ず元へ戻す。"""
        locator = Mock()
        state = {'style': 'width: 300px', 'scrollLeft': 40, 'scrollTop': 80}
        locator.evaluate.side_effect = [state, None]
        path = Path(self.temporary_dir.name) / 'area.png'

        self.executor._screenshot_scroll_area(locator, path, 1500)

        self.assertEqual(locator.evaluate.call_count, 2)
        locator.screenshot.assert_called_once_with(
            path=str(path), timeout=1500, animations='disabled',
        )
        self.assertEqual(locator.evaluate.call_args_list[1].args[1], state)


if __name__ == '__main__':
    unittest.main()
