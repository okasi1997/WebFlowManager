"""タブ、ポップアップ、iframe 対応検索で共有する Playwright 補助処理。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


BROWSER_ARGS = ['--start-maximized']
BROWSER_IGNORED_DEFAULT_ARGS = [
    '--no-sandbox',
    '--enable-automation',
]
HEADLESS_WIDTH = 1920
HEADLESS_HEIGHT = 1080


def browser_args(visible: bool) -> list[str]:
    """表示モードに応じた Chrome 起動引数を返す。"""
    if visible:
        return list(BROWSER_ARGS)
    return [f'--window-size={HEADLESS_WIDTH},{HEADLESS_HEIGHT}']


def browser_context_options(visible: bool) -> dict[str, Any]:
    """表示モードに応じた Playwright コンテキスト設定を返す。"""
    if visible:
        return {'no_viewport': True}
    return {
        'viewport': {'width': HEADLESS_WIDTH, 'height': HEADLESS_HEIGHT},
        'screen': {'width': HEADLESS_WIDTH, 'height': HEADLESS_HEIGHT},
        'device_scale_factor': 1,
    }


def launch_persistent_chrome(
        playwright: Any,
        user_data_dir: Path,
        *,
        visible: bool,
) -> Any:
    """全画面で共通の設定を使用して永続 Chrome コンテキストを起動する。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        'user_data_dir': str(user_data_dir),
        'channel': 'chrome',
        'headless': not visible,
        'args': browser_args(visible),
        **browser_context_options(visible),
    }
    if visible:
        options['ignore_default_args'] = BROWSER_IGNORED_DEFAULT_ARGS
    return playwright.chromium.launch_persistent_context(**options)


def restore_storage_state(context: Any, state_path: Any) -> None:
    """保存済み Cookie と Web Storage を永続コンテキストへ復元する。"""
    if state_path is not None and state_path.is_file():
        context.set_storage_state(str(state_path))


def open_pages(context: Any) -> list[Any]:
    return [page for page in context.pages if not page.is_closed()]


def active_page(reference_page: Any) -> Any:
    """同じコンテキスト内で最後に開かれたページを返す。

    Playwright はポップアップや新規タブを ``context.pages`` の末尾へ追加する。
    ワークフローは元ページをコンテキストの基準として保持し、各操作の直前に
    現在のページを解決する。
    """
    pages = open_pages(reference_page.context)
    return pages[-1] if pages else reference_page


def page_frames(page: Any) -> list[Any]:
    """現在接続されている全フレームをメインフレームから順に返す。"""
    return list(page.frames)


def locators_in_frames(page: Any, factory: Callable[[Any], Any]) -> list[Any]:
    locators: list[Any] = []
    for frame in page_frames(active_page(page)):
        try:
            locators.append(factory(frame))
        except Exception:
            # 動的ページの検査中にフレームが切り離される場合がある。
            continue
    return locators


def is_topmost(locator: Any) -> bool:
    """要素の表示領域内に、操作を受け取れる点が一つ以上あるか確認する。"""
    return bool(locator.evaluate('element => {\n'
        '    const rect = element.getBoundingClientRect();\n'
        '    if (rect.width <= 0 || rect.height <= 0 ||\n'
        '        rect.right <= 0 || rect.bottom <= 0 ||\n'
        '        rect.left >= window.innerWidth || rect.top >= window.innerHeight) return false;\n'
        '    const left = Math.max(0, rect.left), right = Math.min(window.innerWidth, rect.right);\n'
        '    const top = Math.max(0, rect.top), bottom = Math.min(window.innerHeight, rect.bottom);\n'
        '    const points = [\n'
        '        [(left + right) / 2, (top + bottom) / 2],\n'
        '        [left + Math.min(3, (right - left) / 2), (top + bottom) / 2],\n'
        '        [right - Math.min(3, (right - left) / 2), (top + bottom) / 2],\n'
        '        [(left + right) / 2, top + Math.min(3, (bottom - top) / 2)],\n'
        '        [(left + right) / 2, bottom - Math.min(3, (bottom - top) / 2)]\n'
        '    ];\n'
        '    return points.some(([x, y]) => {\n'
        '        const hit = document.elementFromPoint(x, y);\n'
        '        return hit && (hit === element || element.contains(hit));\n'
        '    });\n'
        '}'))


def settle_new_page(page: Any, previous_pages: set[Any], timeout: int) -> Any:
    """同期的に開かれたポップアップまたはタブの遷移開始を待つ。"""
    current = active_page(page)
    if current in previous_pages:
        return current
    try:
        current.wait_for_url(lambda url: url != 'about:blank', timeout=timeout)
    except Exception:
        # 正常なポップアップでも about:blank のままスクリプトで内容を構築する
        # 場合があるため、そのページは操作対象として残す。
        pass
    try:
        current.wait_for_load_state('domcontentloaded', timeout=timeout)
    except Exception:
        pass
    try:
        current.bring_to_front()
    except Exception:
        pass
    return current
