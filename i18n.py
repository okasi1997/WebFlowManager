"""言語リソースの読み込みと文字列翻訳を行う。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SUPPORTED_LANGUAGES = ("ja", "zh")
DEFAULT_LANGUAGE = "ja"
TOKEN_PATTERN = re.compile(r"msg\.\d{4}")
_language = DEFAULT_LANGUAGE
_catalogs: dict[str, dict[str, str]] = {}


def _load_catalog(language: str) -> dict[str, str]:
    # 同じ JSON をウィジェットごとに読み直さないよう、プロセス内でキャッシュする。
    if language not in _catalogs:
        path = Path(__file__).resolve().parent / "locales" / f"{language}.json"
        _catalogs[language] = json.loads(path.read_text(encoding="utf-8"))
    return _catalogs[language]


def set_language(language: str) -> None:
    global _language
    _language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    _load_catalog(_language)


def get_language() -> str:
    return _language


def tr(value: Any) -> Any:
    """完全なキー、および動的文字列内に埋め込まれたキーを翻訳する。"""
    if not isinstance(value, str):
        return value
    catalog = _load_catalog(_language)
    if value in catalog:
        return catalog[value]
    return TOKEN_PATTERN.sub(lambda match: catalog.get(match.group(0), match.group(0)), value)


def tr_language(value: str, language: str) -> str:
    catalog = _load_catalog(language)
    if value in catalog:
        return catalog[value]
    return TOKEN_PATTERN.sub(lambda match: catalog.get(match.group(0), match.group(0)), value)


