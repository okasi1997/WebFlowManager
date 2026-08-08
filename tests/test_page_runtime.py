from __future__ import annotations

import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import call, patch

from browser.auth_session import AuthBrowserSession
from browser.element_picker import DebugBrowserSession, ElementPicker
from browser.page_runtime import BROWSER_ARGS, BROWSER_IGNORED_DEFAULT_ARGS, active_page, close_browser_context, is_topmost, launch_persistent_chrome, locators_in_frames, restore_storage_state, settle_new_page
from browser.picker_scripts import picker_script
from browser.profile_runtime import persistent_profile_dir
from core.executor import WorkflowExecutor
from core.settings import SELECT_FIRST_VALUE


class FakeFrame:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeContext:
    def __init__(self) -> None:
        self.pages = []
        self.restored_cookies = None
        self.init_script = None

    def add_cookies(self, cookies) -> None:
        self.restored_cookies = cookies

    def add_init_script(self, script: str) -> None:
        self.init_script = script


class FakePage:
    def __init__(self, context: FakeContext, url: str, frames: list[FakeFrame] | None=None) -> None:
        self.context = context
        self.url = url
        self.frames = frames or []
        self.front = False
        self.closed = False
        context.pages.append(self)

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_url(self, predicate, timeout: int) -> None:
        if not predicate(self.url):
            raise TimeoutError(timeout)

    def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.load_state = state

    def bring_to_front(self) -> None:
        self.front = True


class FakeChromium:
    def __init__(self, failures: int=0) -> None:
        self.options = None
        self.failures = failures
        self.calls = 0

    def launch_persistent_context(self, **options):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError('profile is temporarily locked')
        self.options = options
        return options


class FakePlaywright:
    def __init__(self, failures: int=0) -> None:
        self.chromium = FakeChromium(failures)


class FakeLocator:
    def __init__(self) -> None:
        self.script = ''

    def evaluate(self, script: str) -> bool:
        self.script = script
        return True


class PageRuntimeTests(unittest.TestCase):
    def test_disabled_group_skips_enabled_inner_events_without_changing_them(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()

            def is_closed(self):
                return False

        page = Page()
        page.context.pages = [page]
        events = [
            {'id': 1, 'position': 1, 'name': 'disabled group', 'action': 'group_start',
             'enabled': 0, 'value': '', 'data_path': ''},
            {'id': 2, 'position': 2, 'name': 'inside', 'action': 'click',
             'enabled': 1, 'selector_type': 'css', 'selector': '#inside', 'value': ''},
            {'id': 3, 'position': 3, 'name': 'disabled group', 'action': 'group_end',
             'enabled': 0},
            {'id': 4, 'position': 4, 'name': 'outside', 'action': 'click',
             'enabled': 1, 'selector_type': 'css', 'selector': '#outside', 'value': ''},
        ]
        executed = []
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._wait_for_salesforce_spinner_if_present = lambda *_args: None
        executor._execute_event = lambda _page, event, *_args: executed.append(event['name'])

        executor._execute_workflow_on_page(
            page, events, {}, Path('.'), None, 'test',
        )

        self.assertEqual(executed, ['outside'])
        self.assertEqual(events[1]['enabled'], 1)

    def test_picker_rejects_selector_with_offscreen_duplicate(self) -> None:
        picker = ElementPicker()
        info = {
            'label': '同じ名前',
            'xpath': '//form[2]//input[1]',
            'role': 'textbox',
        }
        chosen = object()

        def matches(_page, selector_type, _selector):
            return [object(), object()] if selector_type == 'label' else [chosen]

        with (
            patch.object(picker, '_matches_in_page', side_effect=matches),
            patch.object(picker, '_actionable_matches_in_page', return_value=[chosen]),
        ):
            result = picker._choose_unique_locator(object(), info)

        self.assertEqual(result['selector_type'], 'xpath')
        self.assertEqual(result['selector'], '//form[2]//input[1]')

    def test_picker_suggests_action_from_element_tag(self) -> None:
        picker = ElementPicker()
        expected = {'select': 'select', 'input': 'fill', 'button': 'click'}

        for tag, action in expected.items():
            with (
                self.subTest(tag=tag),
                patch.object(picker, '_matches_in_page', return_value=[object()]),
                patch.object(picker, '_actionable_matches_in_page', return_value=[object()]),
            ):
                result = picker._choose_unique_locator(
                    object(), {'tag': tag, 'css': f'#{tag}'},
                )
                self.assertEqual(result['suggested_action'], action)

    def test_get_text_picker_uses_xpath_without_dynamic_text(self) -> None:
        picker = ElementPicker()
        stable_xpath = "//*[@id='price-area']//span"
        stable_css = "span[data-id=price]"
        info = {
            'tag': 'span',
            'role': 'status',
            'name': '1,000円',
            'text': '1,000円',
            'css': 'span',
            'xpath': "//span[normalize-space(.)='1,000円']",
            'stable_xpath': stable_xpath,
            'stable_css': stable_css,
        }
        chosen = object()
        page = object()

        with (
            patch.object(picker, '_matches_in_page', return_value=[chosen]) as matches,
            patch.object(picker, '_actionable_matches_in_page', return_value=[chosen]),
        ):
            result = picker._choose_unique_locator(
                page, info, action='get_text',
            )

        self.assertEqual(result['selector_type'], 'xpath')
        self.assertEqual(result['selector'], stable_xpath)
        self.assertNotIn('1,000円', result['selector'])
        self.assertEqual(result['fallback_selector_type'], 'css')
        self.assertEqual(result['fallback_selector'], stable_css)
        self.assertEqual(
            matches.call_args_list,
            [call(page, 'xpath', stable_xpath), call(page, 'css', stable_css)],
        )

    def test_picker_checks_uniqueness_only_in_selected_frame(self) -> None:
        class Frame:
            parent_frame = None

        picker = ElementPicker()
        frame = Frame()
        chosen = object()

        with (
            patch.object(picker, '_matches_in_page', side_effect=AssertionError),
            patch.object(picker, '_actionable_matches_in_page', side_effect=AssertionError),
            patch.object(picker, '_matches_in_context', return_value=[chosen]) as matches,
            patch.object(picker, '_actionable_matches_in_context', return_value=[chosen]),
        ):
            result = picker._choose_unique_locator(
                object(), {'tag': 'button', 'css': '#save'}, frame,
            )

        self.assertEqual(result['selector'], '#save')
        matches.assert_called_once_with(frame, 'css', '#save')

    def test_event_execution_does_not_run_spinner_scan_while_disabled(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()

            def is_closed(self):
                return False

        page = Page()
        page.context.pages = [page]
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        spinner_checks = []
        executor._wait_for_salesforce_spinner_if_present = (
            lambda *_args: spinner_checks.append(True)
        )
        executor._execute_event = lambda *_args: None

        executor._execute_sequence(
            page,
            [{
                'id': 1, 'position': 1, 'name': 'input', 'action': 'fill',
                'selector_type': 'css', 'selector': '#name', 'value': 'Alice',
                'enabled': 1,
            }],
            {}, Path('.'), None, {}, 'test',
        )

        self.assertEqual(len(spinner_checks), 0)

    def test_spinner_observer_is_limited_to_page_changing_actions(self) -> None:
        executor = WorkflowExecutor(Path('.'), lambda _message: None)

        self.assertFalse(executor._requires_spinner_check({'action': 'fill', 'value': 'A'}))
        self.assertFalse(executor._requires_spinner_check({'action': 'press', 'value': 'ArrowDown'}))
        self.assertTrue(executor._requires_spinner_check({'action': 'click', 'value': ''}))
        self.assertTrue(executor._requires_spinner_check({'action': 'select', 'value': 'A'}))
        self.assertTrue(executor._requires_spinner_check({'action': 'press', 'value': 'Shift+Tab'}))
        self.assertTrue(executor._requires_spinner_check({'action': 'press', 'value': 'Enter'}))

    def test_spinner_observer_waits_only_after_spinner_appears(self) -> None:
        class Page:
            def wait_for_timeout(self, _milliseconds):
                pass

        frame = object()
        states = iter((
            {frame: (1, True)},
            {frame: (2, False)},
        ))
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._prepare_spinner_observers = lambda *_args: next(states)

        with patch('core.executor.active_page', side_effect=lambda page: page):
            executor._wait_for_observed_spinner(
                Page(), {frame: (0, False)}, timeout=1000,
            )

    def test_spinner_observer_checks_only_main_and_saved_target_frame(self) -> None:
        class Frame:
            def __init__(self):
                self.calls = 0

            def evaluate(self, _script):
                self.calls += 1
                return {'revision': 0, 'visible': False}

        main = Frame()
        target = Frame()
        unrelated = Frame()
        page = type('Page', (), {'main_frame': main})()
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._saved_event_frame = lambda *_args: target

        with (
            patch('core.executor.active_page', side_effect=lambda current: current),
            patch('core.executor.page_frames', return_value=[main, target, unrelated]),
        ):
            states = executor._prepare_spinner_observers(
                page, {'iframe_path': '["#target"]'},
            )

        self.assertEqual(set(states), {main, target})
        self.assertEqual(unrelated.calls, 0)

    def test_consecutive_fill_reuses_the_confirmed_frame(self) -> None:
        class Match:
            def __init__(self, frame):
                self._frame = frame
                self.values = []

            def is_visible(self):
                return True

            def scroll_into_view_if_needed(self, timeout):
                self.timeout = timeout

            def fill(self, value):
                self.values.append(value)

        class Locator:
            def __init__(self, match):
                self.match = match

            def count(self):
                return 1

            def nth(self, _index):
                return self.match

        class Frame:
            def __init__(self):
                self.matches = {}

            def is_detached(self):
                return False

            def locator(self, selector):
                return Locator(self.matches[selector])

        class Page:
            def __init__(self, frame):
                self.context = type('Context', (), {})()
                self.context.pages = [self]
                self.frames = [frame]

            def is_closed(self):
                return False

            def set_default_timeout(self, _timeout):
                pass

        frame = Frame()
        first = Match(frame)
        second = Match(frame)
        frame.matches['#second'] = second
        page = Page(frame)
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        global_scans = []

        def full_scan(*_args, **_kwargs):
            global_scans.append(True)
            return first

        executor._event_locator = full_scan
        common = {'action': 'fill', 'selector_type': 'css', 'fallback_selector': '', 'timeout_ms': 1000}

        with patch('core.executor.is_topmost', return_value=True):
            executor._execute_event(page, {**common, 'selector': '#first', 'value': 'A'}, {}, Path('.'))
            executor._execute_event(page, {**common, 'selector': '#second', 'value': 'B'}, {}, Path('.'))

        self.assertEqual(len(global_scans), 1)
        self.assertEqual(first.values, ['A'])
        self.assertEqual(second.values, ['B'])

    def test_input_frame_cache_reads_sync_playwright_locator_frame(self) -> None:
        impl_frame = object()
        frame = type('Frame', (), {'_impl_obj': impl_frame})()

        class Page:
            frames = [frame]

        locator = type(
            'Locator', (),
            {'_impl_obj': type('ImplLocator', (), {'_frame': impl_frame})()},
        )()
        executor = WorkflowExecutor(Path('.'), lambda _message: None)

        with patch('core.executor.active_page', side_effect=lambda page: page):
            executor._remember_input_frame(Page(), locator)

        self.assertIs(executor._input_frame_cache[1], frame)

    def test_saved_iframe_path_is_resolved_only_once(self) -> None:
        class Element:
            calls = 0

            def evaluate(self, _script, _selector):
                self.calls += 1
                return True

        element = Element()

        class ChildFrame:
            child_frames = []

            def frame_element(self):
                return element

            def is_detached(self):
                return False

        child = ChildFrame()
        main = type('MainFrame', (), {'child_frames': [child]})()

        class Page:
            main_frame = main
            frames = [main, child]

        page = Page()
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        event = {'iframe_path': '["#detail-frame"]'}

        with patch('core.executor.active_page', side_effect=lambda current: current):
            self.assertIs(executor._saved_event_frame(page, event), child)
            self.assertIs(executor._saved_event_frame(page, event), child)

        self.assertEqual(element.calls, 1)

    def test_locator_lookup_retries_after_dynamic_target_replacement(self) -> None:
        class Page:
            def __init__(self):
                self.context = type('Context', (), {})()
                self.context.pages = [self]

            def is_closed(self):
                return False

            def wait_for_timeout(self, _milliseconds):
                pass

        class Match:
            def is_visible(self):
                return True

            def scroll_into_view_if_needed(self, timeout):
                pass

        class Locator:
            def __init__(self, fails=False):
                self.fails = fails
                self.match = Match()

            def count(self):
                if self.fails:
                    raise RuntimeError('Locator.count: Target page, context or browser has been closed')
                return 1

            def nth(self, _index):
                return self.match

        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        attempts = iter(([Locator(fails=True)], [Locator()]))
        executor._locators = lambda *_args: next(attempts)

        with patch('core.executor.is_topmost', return_value=True):
            match = executor._unique_locator(Page(), 'css', '#target', timeout=500)

        self.assertIsInstance(match, Match)

    def test_executor_rejects_visible_match_when_offscreen_duplicate_exists(self) -> None:
        class Page:
            def __init__(self):
                self.context = type('Context', (), {})()
                self.context.pages = [self]

            def is_closed(self):
                return False

            def wait_for_timeout(self, _milliseconds):
                pass

        class Match:
            def __init__(self, visible):
                self.visible = visible

            def is_visible(self):
                return self.visible

        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._locator_matches = lambda *_args: [Match(True), Match(False)]

        with self.assertRaisesRegex(RuntimeError, 'msg.0204'):
            executor._unique_locator(Page(), 'label', '同じ名前', timeout=10)

    def test_event_group_timeout_limits_one_attempt(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()
            url = 'https://example.com/'

            def is_closed(self):
                return False

            def wait_for_timeout(self, milliseconds):
                __import__('time').sleep(max(0.05, milliseconds / 1000))

            def set_default_timeout(self, _timeout):
                pass

            def screenshot(self, **_kwargs):
                pass

        page = Page()
        page.context.pages = [page]
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._wait_for_salesforce_spinner_if_present = lambda *_args: None
        events = [
            {'id': 1, 'position': 1, 'name': 'group', 'action': 'group_start',
             'value': '', 'timeout_ms': 1, 'retry_count': 0,
             'retry_interval_ms': 0, 'enabled': 1},
            {'id': 2, 'position': 2, 'name': 'pause', 'action': 'pause',
             'selector_type': 'none', 'selector': '', 'value': '10',
             'timeout_ms': 1000, 'enabled': 1, 'continue_on_error': 0},
            {'id': 3, 'position': 3, 'name': 'group', 'action': 'group_end',
             'enabled': 1},
        ]

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(TimeoutError, 'msg.0576'):
                executor._execute_sequence(
                    page, events, {}, Path(folder), None, {}, 'timeout-test',
                )

    def test_event_group_retry_uses_interval_and_retries_inner_sequence(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()
            url = 'https://example.com/'

            def __init__(self):
                self.waits = []

            def is_closed(self):
                return False

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

            def screenshot(self, **_kwargs):
                pass

        page = Page()
        page.context.pages = [page]
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._wait_for_salesforce_spinner_if_present = lambda *_args: None
        attempts = []

        def execute(*_args):
            attempts.append(True)
            if len(attempts) < 3:
                raise RuntimeError('temporary')

        executor._execute_event = execute
        events = [
            {'id': 1, 'position': 1, 'name': 'group', 'action': 'group_start',
             'value': '2', 'timeout_ms': 10000, 'retry_count': 2,
             'retry_interval_ms': 750, 'enabled': 1},
            {'id': 2, 'position': 2, 'name': 'save', 'action': 'click',
             'selector_type': 'css', 'selector': '#save', 'value': '',
             'timeout_ms': 1000, 'enabled': 1, 'continue_on_error': 0},
            {'id': 3, 'position': 3, 'name': 'group', 'action': 'group_end',
             'enabled': 1},
        ]

        with tempfile.TemporaryDirectory() as folder:
            executor._execute_sequence(
                page, events, {}, Path(folder), None, {}, 'group-test',
            )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(page.waits, [750, 750])

    def test_single_event_retry_uses_configured_interval(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()

            def __init__(self):
                self.waits = []

            def is_closed(self):
                return False

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        page.context.pages = [page]
        messages = []
        executor = WorkflowExecutor(Path('.'), messages.append)
        attempts = []
        spinner_checks = []
        executor._wait_for_salesforce_spinner_if_present = (
            lambda *_args: spinner_checks.append(True)
        )

        def execute(*_args):
            attempts.append(True)
            if len(attempts) < 3:
                raise RuntimeError('temporary')

        executor._execute_event = execute
        executor._execute_sequence(
            page,
            [{
                'id': 1, 'position': 1, 'name': 'retry me', 'action': 'click',
                'selector_type': 'css', 'selector': '#save', 'value': '',
                'enabled': 1, 'retry_count': 2, 'retry_interval_ms': 750,
            }],
            {}, Path('.'), None, {}, 'test',
        )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(page.waits, [750, 750])
        self.assertEqual(len(spinner_checks), 0)
        self.assertEqual(sum('イベント再試行' in message for message in messages), 2)

    def test_wait_hidden_waits_until_all_matching_elements_are_hidden(self) -> None:
        class Context:
            pages = []

        class Element:
            def __init__(self, page):
                self.page = page

            def is_visible(self):
                return self.page.polls < 3

        class Collection:
            def __init__(self, page):
                self.element = Element(page)

            def count(self):
                return 1

            def nth(self, _index):
                return self.element

        class Page:
            context = Context()
            polls = 0

            def is_closed(self):
                return False

            def wait_for_timeout(self, _milliseconds):
                self.polls += 1

        page = Page()
        page.context.pages = [page]
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._locators = lambda *_args: [Collection(page)]

        executor._wait_until_hidden(page, 'css', '.slds-spinner', 1000)

        self.assertEqual(page.polls, 3)

    def test_wait_visible_accepts_the_first_visible_element_from_multiple_matches(self) -> None:
        class Context:
            pages = []

        class Element:
            def __init__(self, visible):
                self.visible = visible

            def is_visible(self):
                return self.visible

        class Collection:
            elements = [Element(False), Element(True)]

            def count(self):
                return len(self.elements)

            def nth(self, index):
                return self.elements[index]

        class Page:
            context = Context()

            def is_closed(self):
                return False

            def wait_for_timeout(self, _milliseconds):
                raise AssertionError('visible element should complete immediately')

        page = Page()
        page.context.pages = [page]
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._locators = lambda *_args: [Collection()]

        executor._wait_until_ready(
            page,
            {'selector_type': 'css', 'fallback_selector_type': 'none'},
            '.item', '', 'visible', 1000,
        )

    def test_wait_operable_does_not_interact_with_button(self) -> None:
        class Element:
            def is_visible(self):
                return True

            def is_enabled(self):
                return True

            def evaluate(self, _script):
                return 'click'

            def click(self, **_kwargs):
                raise AssertionError('待機判定中にボタンを操作してはいけない')

        element = Element()

        with patch('core.executor.is_topmost', return_value=True) as topmost:
            self.assertTrue(
                WorkflowExecutor._matches_wait_condition(
                    element, 'operable', time.monotonic() + 1,
                )
            )
        topmost.assert_called_once_with(element)

    def test_wait_operable_checks_editability_for_input(self) -> None:
        class Element:
            def is_visible(self):
                return True

            def is_enabled(self):
                return True

            def evaluate(self, _script):
                return 'input'

            def is_editable(self):
                return True

        element = Element()
        with patch('core.executor.is_topmost', return_value=True) as topmost:
            self.assertTrue(
                WorkflowExecutor._matches_wait_condition(
                    element, 'operable', time.monotonic() + 1,
                )
            )
        topmost.assert_called_once_with(element)

    def test_wait_hidden_prefers_the_saved_iframe(self) -> None:
        class Match:
            def is_visible(self):
                return False

        class Locator:
            def count(self):
                return 1

            def nth(self, _index):
                return Match()

        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        frame = object()
        executor._saved_event_frame = lambda *_args: frame
        executor._locator = lambda target, *_args: Locator() if target is frame else None
        executor._visible_locator_count = lambda *_args: self.fail('全 frame 検索は不要です')

        page = object()
        with patch('core.executor.active_page', side_effect=lambda current: current):
            executor._wait_until_hidden(
                page, 'css', '.loading', 1000, {'iframe_path': '["#frame"]'},
            )

    def test_file_input_prefers_the_saved_iframe_even_when_hidden(self) -> None:
        match = object()

        class Locator:
            def count(self):
                return 1

            def nth(self, _index):
                return match

        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        frame = object()
        executor._saved_event_frame = lambda *_args: frame
        executor._locator = lambda target, *_args: Locator() if target is frame else None
        executor._locators = lambda *_args: self.fail('全 frame 検索は不要です')

        result = executor._file_input_locator(
            object(),
            {
                'selector_type': 'css',
                'fallback_selector_type': 'none',
                'iframe_path': '["#frame"]',
            },
            'input[type=file]', '', 1000,
        )

        self.assertIs(result, match)

    def test_event_log_details_include_effective_operation_values(self) -> None:
        self.assertEqual(
            WorkflowExecutor._event_log_detail(
                {
                    'action': 'fill',
                    'selector_type': 'css',
                    'selector': '#account',
                    'value': '${name}',
                },
                {'name': 'Alice'},
            ),
            'fill selector[css]="#account" value="Alice"',
        )
        self.assertEqual(
            WorkflowExecutor._event_log_detail(
                {
                    'action': 'goto',
                    'selector_type': 'none',
                    'selector': '',
                    'value': 'https://example.com/${id}',
                },
                {'id': '42'},
            ),
            'goto url="https://example.com/42"',
        )

    def test_unique_offscreen_element_is_scrolled_before_topmost_check(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()

            def is_closed(self):
                return False

            def wait_for_timeout(self, _milliseconds):
                pass

        class Locator(FakeLocator):
            scrolled = False

            def is_visible(self):
                return True

            def scroll_into_view_if_needed(self, timeout):
                self.scrolled = True

            def evaluate(self, script):
                self.script = script
                return self.scrolled

        page = Page()
        page.context.pages = [page]
        locator = Locator()
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._locators = lambda _page, _selector_type, _selector: [
            type('Collection', (), {
                'count': lambda self: 1,
                'nth': lambda self, _index: locator,
            })()
        ]

        self.assertIs(executor._unique_locator(page, 'css', '#target', 100), locator)
        self.assertTrue(locator.scrolled)

    def test_select_can_choose_the_first_option(self) -> None:
        class Context:
            pages = []

        class Page:
            context = Context()

            def is_closed(self):
                return False

            def set_default_timeout(self, _timeout):
                pass

        class Locator:
            selected = None

            def select_option(self, *args, **kwargs):
                self.selected = (args, kwargs)

        page = Page()
        page.context.pages = [page]
        locator = Locator()
        executor = WorkflowExecutor(Path('.'), lambda _message: None)
        executor._event_locator = lambda *_args, **_kwargs: locator
        executor._execute_event(
            page,
            {
                'action': 'select',
                'selector': '#items',
                'fallback_selector': '',
                'value': SELECT_FIRST_VALUE,
                'timeout_ms': 100,
            },
            {},
            Path('.'),
        )

        self.assertEqual(locator.selected, ((), {'index': 0}))

    def test_closing_an_already_closed_browser_is_idempotent(self) -> None:
        class ClosedContext:
            def close(self) -> None:
                raise RuntimeError(
                    'BrowserContext.close: Target page, context or browser has been closed'
                )

        close_browser_context(ClosedContext())

    def test_browser_close_still_reports_unexpected_errors(self) -> None:
        class BrokenContext:
            def close(self) -> None:
                raise RuntimeError('profile cleanup failed')

        with self.assertRaisesRegex(RuntimeError, 'profile cleanup failed'):
            close_browser_context(BrokenContext())

    def test_browser_close_methods_resolve_worker_callbacks_lazily(self) -> None:
        auth = AuthBrowserSession.__new__(AuthBrowserSession)
        auth_closed = []

        def auth_submit(task, timeout=None):
            auth._close_browser = lambda: auth_closed.append(True)
            return task()

        auth._submit = auth_submit
        auth.close_browser()
        self.assertEqual(auth_closed, [True])

        debug = DebugBrowserSession.__new__(DebugBrowserSession)
        debug._cancel_requested = type(
            'CancelFlag',
            (),
            {'set': lambda self: None},
        )()
        debug_closed = []

        def debug_submit(task, timeout=None):
            debug._dispose = lambda: debug_closed.append(True)
            return task()

        debug._submit = debug_submit
        debug.close_browser()
        self.assertEqual(debug_closed, [True])

    def test_picker_script_can_be_reinstalled_and_uses_shadow_event_path(self) -> None:
        script = picker_script('run-123', '待機', '選択中')
        self.assertIn('__webFlowPickerCleanup', script)
        self.assertIn('"run-123"', script)
        self.assertIn('event.composedPath()', script)
        self.assertIn("'data-target-selection-name', 'data-testid', 'data-id', 'name'", script)
        self.assertIn('stable_xpath: xpathCandidate(element, false)', script)
        self.assertIn('//${tag}[@${attr}=${xpathLiteral(value)}]', script)
        self.assertIn('//${tag}[normalize-space(.)=${xpathLiteral(exactText)}]', script)
        self.assertIn('const elementTextCandidates = (element)', script)
        self.assertIn('element.textContent ||', script)
        self.assertIn('element.value ||', script)
        self.assertIn("unique(`//${parts.join('/')}`)", script)
        self.assertIn('const descendants = parts.slice(1)', script)
        self.assertIn('descendants.slice(-length)', script)
        self.assertIn('unique(`${anchor}//${suffix}`)', script)
        self.assertIn('const anchorCandidate = (node)', script)
        self.assertIn('const scopedTargetCandidates = (node)', script)
        self.assertIn('const semanticAnchoredCandidate = ()', script)
        self.assertIn('const resolvesTarget = (xpath)', script)
        self.assertIn('normalize-space(.)=${xpathLiteral(text)}', script)
        self.assertIn('if (resolvesTarget(candidate)) return candidate', script)
        self.assertIn("'data-target-selection-name', 'data-testid', 'data-id', 'name', 'role'", script)
        self.assertIn('//${nodeTag}[@${attr}=${xpathLiteral(value)}]', script)
        self.assertIn('unique(`${anchor}//${targetCandidate}`)', script)
        self.assertIn('const plainParts = []', script)
        self.assertIn('unique(`${anchor}//${suffix}`)', script)
        self.assertIn('actionableSelector', script)
        self.assertIn('"待機"', script)
        self.assertIn('"選択中"', script)

    def test_topmost_check_uses_the_elements_shadow_root(self) -> None:
        locator = FakeLocator()
        self.assertTrue(is_topmost(locator))
        self.assertIn('element.getRootNode()', locator.script)
        self.assertIn('root.elementFromPoint', locator.script)

    def test_workflow_browser_uses_normal_chrome_arguments(self) -> None:
        for visible in (True, False):
            arguments = WorkflowExecutor._browser_args(visible)
            self.assertNotIn('--ignore-certificate-errors', arguments)
            self.assertNotIn('--allow-insecure-localhost', arguments)
        self.assertEqual(WorkflowExecutor._browser_args(True), BROWSER_ARGS)
        self.assertEqual(
            BROWSER_IGNORED_DEFAULT_ARGS,
            ['--no-sandbox', '--enable-automation'],
        )

    def test_workflow_context_does_not_force_site_permissions(self) -> None:
        for visible in (True, False):
            options = WorkflowExecutor._context_options(visible)
            self.assertNotIn('permissions', options)
            self.assertNotIn('ignore_https_errors', options)

    def test_all_browser_entry_points_share_persistent_launch_options(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            playwright = FakePlaywright()
            profile = Path(folder) / 'profile'
            options = launch_persistent_chrome(playwright, profile, visible=True)
            self.assertTrue(profile.is_dir())
            self.assertEqual(options['user_data_dir'], str(profile))
            self.assertEqual(options['channel'], 'chrome')
            self.assertFalse(options['headless'])
            self.assertTrue(options['no_viewport'])
            self.assertEqual(options['ignore_default_args'], BROWSER_IGNORED_DEFAULT_ARGS)

    def test_persistent_launch_retries_a_temporarily_locked_profile(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            playwright = FakePlaywright(failures=1)
            options = launch_persistent_chrome(
                playwright,
                Path(folder) / 'profile',
                visible=True,
            )
            self.assertEqual(playwright.chromium.calls, 2)
            self.assertFalse(options['headless'])

    def test_saved_state_maps_to_application_profile_directory(self) -> None:
        project = Path('C:/work/project')
        self.assertEqual(
            persistent_profile_dir(project, project / 'data' / 'browser_state.json'),
            project / 'data' / 'chrome_profiles' / 'default',
        )
        self.assertEqual(
            persistent_profile_dir(project, project / 'data' / 'browser_states' / 'sales.json'),
            project / 'data' / 'chrome_profiles' / 'sales',
        )
        self.assertIsNone(persistent_profile_dir(project, None))

    def test_saved_login_state_is_restored_into_persistent_context(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / 'browser_state.json'
            state_path.write_text(
                '{"cookies": [{"name": "sid", "value": "1", "domain": ".example.test", "path": "/"}], '
                '"origins": [{"origin": "https://example.test", '
                '"localStorage": [{"name": "token", "value": "saved"}]}]}',
                encoding='utf-8',
            )
            context = FakeContext()
            restore_storage_state(context, state_path)
            self.assertEqual(context.restored_cookies[0]['name'], 'sid')
            self.assertIn('https://example.test', context.init_script)
            self.assertIn('localStorage.setItem', context.init_script)

    def test_active_page_follows_newest_open_tab(self) -> None:
        context = FakeContext()
        original = FakePage(context, 'https://example.test/one')
        popup = FakePage(context, 'https://example.test/two')
        self.assertIs(active_page(original), popup)
        popup.closed = True
        self.assertIs(active_page(original), original)

    def test_locator_factory_visits_main_page_and_iframes(self) -> None:
        context = FakeContext()
        frames = [FakeFrame('main'), FakeFrame('payment-frame')]
        page = FakePage(context, 'https://example.test', frames)
        self.assertEqual(locators_in_frames(page, lambda frame: frame.name), ['main', 'payment-frame'])

    def test_new_popup_is_brought_to_front(self) -> None:
        context = FakeContext()
        original = FakePage(context, 'https://example.test/one')
        previous = {original}
        popup = FakePage(context, 'https://example.test/two')
        self.assertIs(settle_new_page(original, previous, 1000), popup)
        self.assertTrue(popup.front)

    def test_script_populated_about_blank_popup_remains_usable(self) -> None:
        context = FakeContext()
        original = FakePage(context, 'https://example.test/one')
        previous = {original}
        popup = FakePage(context, 'about:blank')
        self.assertIs(settle_new_page(original, previous, 1), popup)
        self.assertTrue(popup.front)


if __name__ == '__main__':
    unittest.main()
