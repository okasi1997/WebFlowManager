"""要素選択と実行で共用する Locator の生成・検索処理。"""
from __future__ import annotations

from typing import Any

from browser.page_runtime import is_topmost, locators_in_frames


def build_locator(context: Any, selector_type: str, selector: str) -> Any:
    """指定された検索方式から Playwright Locator を生成する。"""
    if selector_type == 'role':
        role, separator, name = selector.partition('|')
        return context.get_by_role(role.strip(), name=name.strip() if separator else None)
    if selector_type == 'label':
        return context.get_by_label(selector, exact=True)
    if selector_type == 'placeholder':
        return context.get_by_placeholder(selector, exact=True)
    if selector_type == 'text':
        return context.get_by_text(selector, exact=True)
    if selector_type == 'css':
        return context.locator(selector)
    if selector_type == 'xpath':
        return context.locator(f'xpath={selector}')
    raise ValueError(f'Action requires a selector: {selector_type}')


def locators_across_frames(page: Any, selector_type: str, selector: str) -> list[Any]:
    """メイン画面と iframe ごとの Locator を返す。"""
    return locators_in_frames(page, lambda frame: build_locator(frame, selector_type, selector))


def matches_across_frames(page: Any, selector_type: str, selector: str) -> list[Any]:
    """全 frame 内の一致要素を DOM 順で返す。"""
    return [
        locator.nth(index)
        for locator in locators_across_frames(page, selector_type, selector)
        for index in range(locator.count())
    ]


def visible_matches(locator: Any) -> list[Any]:
    """Locator のうち表示中の要素だけを返す。"""
    return [locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible()]


def actionable_matches(locator: Any) -> list[Any]:
    """表示中かつ他要素に覆われていない要素だけを返す。"""
    return [item for item in visible_matches(locator) if is_topmost(item)]


def actionable_matches_across_frames(
        page: Any, selector_type: str, selector: str,
) -> list[Any]:
    """全 frame から操作可能な一致要素を返す。"""
    return [
        item
        for locator in locators_across_frames(page, selector_type, selector)
        for item in actionable_matches(locator)
    ]
