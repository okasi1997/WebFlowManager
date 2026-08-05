from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from browser.auth_session import AuthBrowserSession
from browser.element_picker import DebugBrowserSession
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
        self.assertEqual(len(spinner_checks), 4)
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
        executor._event_locator = lambda *_args: locator
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
