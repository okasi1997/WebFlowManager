import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

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

    def test_get_text_can_extract_only_the_regex_match(self) -> None:
        page = Mock()
        page.is_closed.return_value = False
        locator = Mock()
        locator.text_content.return_value = '試算ID\nEST20260821000000605\n期限日'
        event = {
            'action': 'get_text', 'value': 'estimate_id',
            'selector_type': 'css', 'selector': '#estimate-id',
            'fallback_selector_type': 'none', 'fallback_selector': '',
            'timeout_ms': 1000, 'success_json': '',
            'text_extract_regex': r'(?m)^EST.*$',
        }
        variables: dict[str, str] = {}

        with (
            patch('core.executor.active_page', return_value=page),
            patch.object(self.executor, '_fast_event_locator', return_value=locator),
        ):
            captured = self.executor._execute_event(
                page, event, variables, Path(self.temporary_dir.name),
            )

        self.assertEqual(captured, 'EST20260821000000605')
        self.assertEqual(variables['estimate_id'], captured)

    def test_screenshot_locator_searches_only_the_saved_main_frame(self) -> None:
        """iframe パスが空なら、同じ selector を持つ iframe を検索対象に含めない。"""
        page = Mock()
        main_frame = Mock()
        page.main_frame = main_frame
        match = Mock()
        match.is_visible.return_value = True
        locator = Mock()
        locator.count.return_value = 1
        locator.nth.return_value = match
        event = {
            'selector_type': 'css', 'fallback_selector_type': 'xpath',
            'iframe_path': '',
        }

        with (
            patch('core.executor.active_page', return_value=page),
            patch.object(self.executor, '_locator', return_value=locator) as build_locator,
            patch.object(self.executor, '_event_frame') as event_frame,
        ):
            result = self.executor._screenshot_event_locator(
                page, event, 'html', '//html', 1000,
            )

        self.assertIs(result, match)
        build_locator.assert_called_once_with(main_frame, 'css', 'html')
        event_frame.assert_not_called()

    def test_scroll_area_screenshot_restores_element_state(self) -> None:
        """分割キャプチャーではレイアウトを変更せず、最後にスクロール位置を戻す。"""
        locator = Mock()
        handle = Mock()
        locator.element_handle.return_value = handle
        locator.page.screenshot.return_value = b'png'
        metrics = {
            'clientWidth': 100, 'clientHeight': 80,
            'scrollWidth': 100, 'scrollHeight': 80,
            'scrollLeft': 40, 'scrollTop': 20,
            'clientLeft': 0, 'clientTop': 0,
        }
        handle.bounding_box.return_value = {'x': 10, 'y': 15, 'width': 100, 'height': 80}
        original = {'x': 40, 'y': 20, 'behavior': '', 'behaviorPriority': ''}
        handle.evaluate.side_effect = [
            original, None, None, metrics, {'x': 0, 'y': 0}, None, None,
        ]
        path = Path(self.temporary_dir.name) / 'area.png'
        tile = Mock()
        tile.isNull.return_value = False
        tile.width.return_value = 100
        tile.height.return_value = 80
        canvas = Mock()
        canvas.width.return_value = 100
        canvas.height.return_value = 80
        canvas.save.return_value = True

        with (
            patch('core.executor.QImage') as image_class,
            patch('core.executor.QPainter') as painter_class,
        ):
            image_class.fromData.return_value = tile
            image_class.return_value = canvas
            image_class.Format.Format_ARGB32 = 1
            self.executor._screenshot_scroll_area(locator, path, 1500)

        locator.page.screenshot.assert_called_once_with(
            clip={'x': 10, 'y': 15, 'width': 100, 'height': 80},
            animations='disabled', timeout=1500,
        )
        reset_script = handle.evaluate.call_args_list[1].args[0]
        self.assertIn('element.scrollTop = 0', reset_script)
        restore_point = handle.evaluate.call_args_list[-1].args[1]
        self.assertEqual((restore_point['x'], restore_point['y']), (40, 20))
        canvas.save.assert_called_once_with(str(path), 'PNG')

    def test_screenshot_shell_keeps_outer_area_once_around_expanded_content(self) -> None:
        """祖先範囲の外枠を残し、スクロール表示部だけを展開画像へ置き換える。"""
        shell = QImage(40, 30, QImage.Format.Format_ARGB32)
        shell.fill(QColor('black'))
        content = QImage(60, 50, QImage.Format.Format_ARGB32)
        content.fill(QColor('white'))

        result = self.executor._compose_screenshot_shell(
            shell, content,
            {'width': 40, 'height': 30},
            {'x': 10, 'y': 5, 'width': 20, 'height': 10},
        )

        self.assertEqual((result.width(), result.height()), (80, 70))
        self.assertEqual(result.pixelColor(0, 0), QColor('black'))
        self.assertEqual(result.pixelColor(10, 5), QColor('white'))
        self.assertEqual(result.pixelColor(79, 69), QColor('black'))


if __name__ == '__main__':
    unittest.main()
