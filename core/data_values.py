"""Data 値の入出力で共用する正規化処理。"""
from __future__ import annotations

from typing import Any


def strip_unicode_whitespace(value: Any) -> Any:
    """文字列と入れ子構造から先頭・末尾の Unicode 空白を除去する。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {key: strip_unicode_whitespace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_unicode_whitespace(item) for item in value]
    return value


def normalize_data_record(record: dict[str, Any]) -> dict[str, Any]:
    """Data レコードの名称、概要、実行設定、値を同じ規則で正規化する。"""
    cleaned = dict(record)
    for field in ('name', 'summary', 'execution_group'):
        if field in cleaned:
            cleaned[field] = str(cleaned[field]).strip()
    if 'data' in cleaned:
        cleaned['data'] = strip_unicode_whitespace(cleaned['data'])
    return cleaned
