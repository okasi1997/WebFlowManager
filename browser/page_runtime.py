"""タブ、ポップアップ、iframe 対応検索で共有する Playwright 補助処理。"""
from __future__ import annotations

from typing import Any, Callable


BROWSER_ARGS = ['--start-maximized']
BROWSER_IGNORED_DEFAULT_ARGS = ['--no-sandbox']


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
