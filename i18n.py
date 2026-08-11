"""言語リソースの読み込みと文字列翻訳を行う。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SUPPORTED_LANGUAGES = ("ja", "zh")
DEFAULT_LANGUAGE = "ja"
_language = DEFAULT_LANGUAGE
_catalogs: dict[str, dict[str, str]] = {}
_ja_value_keys: dict[str, str] | None = None


def _load_catalog(language: str) -> dict[str, str]:
    # 同じ JSON をウィジェットごとに読み直さないよう、プロセス内でキャッシュする。
    if language not in _catalogs:
        path = Path(__file__).resolve().parent / "locales" / f"{language}.json"
        _catalogs[language] = json.loads(path.read_text(encoding="utf-8"))
    return _catalogs[language]


def _catalog_key_for_source(value: str) -> str | None:
    """既存の日本語訳から再利用可能な翻訳キーを逆引きする。"""
    global _ja_value_keys
    if _ja_value_keys is None:
        _ja_value_keys = {text: key for key, text in _load_catalog("ja").items()}
    return _ja_value_keys.get(value)


def _translate_source_fragments(value: str, language: str) -> str:
    """変数を含む文に使う接頭辞・接尾辞だけを安全に置換する。"""
    translated = value
    for source, target in sorted(
        (
            item for item in _load_catalog(language).items()
            if not item[0].startswith('msg.')
        ),
        key=lambda item: len(item[0]), reverse=True,
    ):
        # 完全文との誤置換を避け、空白や区切り記号を含む断片だけを対象にする。
        if source != source.strip() or source.startswith((':', '：')) or source.endswith((':', '：')):
            translated = translated.replace(source, target)
    return translated


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
    catalog_key = _catalog_key_for_source(value)
    if catalog_key is not None:
        return catalog.get(catalog_key, value)
    return _translate_source_fragments(value, _language)


def tr_language(value: str, language: str) -> str:
    catalog = _load_catalog(language)
    if value in catalog:
        return catalog[value]
    catalog_key = _catalog_key_for_source(value)
    if catalog_key is not None:
        return catalog.get(catalog_key, value)
    return _translate_source_fragments(value, language)


