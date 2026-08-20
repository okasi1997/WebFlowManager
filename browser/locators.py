"""要素選択と実行で共用する Locator の生成・検索処理。"""
from __future__ import annotations

import json
import re
from typing import Any

from browser.page_runtime import is_topmost, locators_in_frames


def multi_path_steps(selector: str) -> list[dict[str, str]]:
    """保存済み多段パスから、画面表示に必要な最小情報だけを取得する。"""
    try:
        data = json.loads(selector)
        steps = data.get('steps', []) if isinstance(data, dict) else []
        return [step for step in steps if isinstance(step, dict)]
    except (TypeError, ValueError):
        return []


def xpath_literal(value: str) -> str:
    """任意の文字列を XPath 1.0 で利用できるリテラルへ変換する。"""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat('" + value.replace("'", "',\"'\",'") + "')"


def _render_path_values(data: dict[str, Any]) -> dict[str, Any]:
    """編集可能な各段の値を、保存済み XPath テンプレートへ安全に埋め込む。"""
    steps = data.get('steps', [])
    parameters = data.get('parameters', {})

    def expand_parameters(value: str) -> str:
        """$1～$99 を解決し、$$1 は文字どおりの $1 として残す。"""
        escaped = '\0WFM_DOLLAR\0'
        value = value.replace('$$', escaped)

        def replace(match: re.Match[str]) -> str:
            number = match.group(1)
            parameter = parameters.get(number) if isinstance(parameters, dict) else None
            if not isinstance(parameter, dict) or 'resolved_value' not in parameter:
                raise ValueError(f'Undefined multi-path parameter: ${number}')
            return str(parameter['resolved_value'])

        return re.sub(r'\$(\d{1,2})(?!\d)', replace, value).replace(escaped, '$')

    replacements = {
        f'__WFM_STEP_{index}__': xpath_literal(expand_parameters(str(step.get('value', ''))))
        for index, step in enumerate(steps)
        if isinstance(step, dict)
    }

    def render(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: render(item) for key, item in value.items()}
        if isinstance(value, list):
            return [render(item) for item in value]
        if isinstance(value, str):
            for token, literal in replacements.items():
                value = value.replace(token, literal)
        return value

    return render(data)


def selector_preview(selector_type: str, selector: str) -> str:
    """ログ用に、パラメーター展開後の実際の検出パスを返す。"""
    if selector_type != 'path':
        return selector
    data = json.loads(selector)
    rendered = _render_path_values(data) if isinstance(data, dict) else {}
    resolved = rendered.get('resolved', {}) if isinstance(rendered, dict) else {}
    if isinstance(resolved, dict) and resolved.get('selector'):
        return str(resolved['selector'])
    mapping = rendered.get('row_mapping', {}) if isinstance(rendered, dict) else {}
    if isinstance(mapping, dict) and mapping:
        source = mapping.get('source', {})
        target_table = mapping.get('target_table', {})
        target = mapping.get('target', {})
        return (
            f'source={source.get("selector", "")} -> '
            f'target_table={target_table.get("selector", "")} -> '
            f'target={target.get("selector", "")} (same_row_index)'
        )
    return selector


def build_locator(context: Any, selector_type: str, selector: str) -> Any:
    """指定された検索方式から Playwright Locator を生成する。"""
    if selector_type == 'path':
        data = json.loads(selector)
        if isinstance(data, dict):
            data = _render_path_values(data)
        if isinstance(data, dict) and isinstance(data.get('row_mapping'), dict):
            return _build_row_mapping_locator(context, data['row_mapping'])
        # 通常の範囲パスも最終的には既存 locator へ変換し、実行・強調表示・
        # 待機で同じ検索処理を共有する。
        resolved = data.get('resolved', {}) if isinstance(data, dict) else {}
        resolved_type = str(resolved.get('selector_type', ''))
        resolved_selector = str(resolved.get('selector', ''))
        if resolved_type == 'path' or not resolved_type or not resolved_selector:
            raise ValueError('Invalid multi-step path')
        return build_locator(context, resolved_type, resolved_selector)
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


def _build_row_mapping_locator(context: Any, mapping: dict[str, Any]) -> Any:
    """基準行の現在 index を、同じ外側範囲にある対象表へ動的に適用する。"""
    source = mapping.get('source', {})
    target_table = mapping.get('target_table', {})
    target = mapping.get('target', {})
    source_locator = build_locator(
        context, str(source.get('selector_type', '')), str(source.get('selector', '')),
    )
    table_locator = build_locator(
        context, str(target_table.get('selector_type', '')),
        str(target_table.get('selector', '')),
    )
    if source_locator.count() != 1 or table_locator.count() != 1:
        raise ValueError('Invalid row mapping scope')
    # thead を混ぜず、基準行と同じ親要素内にある tr だけで index を計算する。
    row_index = int(source_locator.nth(0).evaluate("""element => {
      const row = element.closest('tr');
      if (!row || !row.parentElement) return -1;
      return [...row.parentElement.children]
        .filter(item => item.tagName === 'TR').indexOf(row);
    }"""))
    if row_index < 0:
        raise ValueError('Invalid source row')
    rows = table_locator.nth(0).locator(str(
        mapping.get('row_selector', ':scope > tbody > tr, :scope > tr')
    ))
    if row_index >= rows.count():
        raise ValueError('Mapped row is out of range')
    target_row = rows.nth(row_index)
    return build_locator(
        target_row, str(target.get('selector_type', '')), str(target.get('selector', '')),
    )


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
