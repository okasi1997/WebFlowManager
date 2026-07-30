from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from browser.auth_session import AuthBrowserSession
from browser.element_picker import DebugBrowserSession
from browser.page_runtime import BROWSER_ARGS, BROWSER_IGNORED_DEFAULT_ARGS, active_page, is_topmost, launch_persistent_chrome, locators_in_frames, restore_storage_state, settle_new_page
from browser.picker_scripts import picker_script
from browser.profile_runtime import persistent_profile_dir
from core.executor import WorkflowExecutor


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
    def test_browser_close_methods_resolve_worker_callbacks_lazily(self) -> None:
        auth = AuthBrowserSession.__new__(AuthBrowserSession)
        auth_closed = []

        def auth_submit(task):
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

        def debug_submit(task):
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
