"""言語リソースの読込と Tk/ttk への透過的な翻訳適用を行う。"""
from __future__ import annotations

import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any


SUPPORTED_LANGUAGES = ("ja", "zh")
DEFAULT_LANGUAGE = "ja"
TOKEN_PATTERN = re.compile(r"msg\.\d{4}")
_language = DEFAULT_LANGUAGE
_catalogs: dict[str, dict[str, str]] = {}
_installed = False


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


def _translate_values(values: Any) -> Any:
    if not isinstance(values, (tuple, list)):
        return values
    return tuple(tr(value) for value in values)


def install_tk_translation() -> None:
    """Tk の生成・更新経路へ翻訳処理を一度だけ組み込む。"""
    global _installed
    if _installed:
        return
    _installed = True

    original_options = tk.Misc._options

    def options(self: tk.Misc, cnf: Any, kw: Any = None) -> Any:
        merged = dict(cnf or {})
        if kw:
            merged.update(kw)
        if "text" in merged:
            merged["text"] = tr(merged["text"])
        return original_options(self, merged)

    tk.Misc._options = options
    original_ttk_options = ttk._format_optdict

    def ttk_options(optdict: Any, script: bool = False, ignore: Any = None) -> Any:
        # ttk は通常の Tk と異なるオプション整形関数を通るため、個別に差し替える。
        translated = dict(optdict)
        if "text" in translated:
            translated["text"] = tr(translated["text"])
        if "values" in translated:
            translated["values"] = _translate_values(translated["values"])
        return original_ttk_options(translated, script, ignore)

    ttk._format_optdict = ttk_options
    original_title = tk.Wm.wm_title

    def window_title(self: tk.Wm, value: Any = None) -> Any:
        return original_title(self, tr(value) if value is not None else value)

    tk.Wm.wm_title = window_title
    tk.Wm.title = window_title
    original_heading = ttk.Treeview.heading

    def heading(self: ttk.Treeview, column: Any, option: Any = None, **kw: Any) -> Any:
        if "text" in kw:
            kw["text"] = tr(kw["text"])
        return original_heading(self, column, option, **kw)

    ttk.Treeview.heading = heading
    original_insert = ttk.Treeview.insert

    def insert(self: ttk.Treeview, parent: Any, index: Any, iid: Any = None, **kw: Any) -> Any:
        if "text" in kw:
            kw["text"] = tr(kw["text"])
        if "values" in kw:
            kw["values"] = _translate_values(kw["values"])
        return original_insert(self, parent, index, iid, **kw)

    ttk.Treeview.insert = insert
    original_item = ttk.Treeview.item

    def item(self: ttk.Treeview, item_id: Any, option: Any = None, **kw: Any) -> Any:
        if "text" in kw:
            kw["text"] = tr(kw["text"])
        if "values" in kw:
            kw["values"] = _translate_values(kw["values"])
        return original_item(self, item_id, option, **kw)

    ttk.Treeview.item = item
    original_show = messagebox._show

    def show(title: Any = None, message: Any = None, *args: Any, **kw: Any) -> Any:
        return original_show(tr(title), tr(message), *args, **kw)

    messagebox._show = show

    def wrap_file_dialog(function: Any) -> Any:
        # OS 標準ファイルダイアログの種類名も現在言語へ変換する。
        def wrapped(*args: Any, **kw: Any) -> Any:
            if "title" in kw:
                kw["title"] = tr(kw["title"])
            if "filetypes" in kw:
                kw["filetypes"] = tuple((tr(label), pattern) for label, pattern in kw["filetypes"])
            return function(*args, **kw)
        return wrapped

    filedialog.askopenfilename = wrap_file_dialog(filedialog.askopenfilename)
    filedialog.asksaveasfilename = wrap_file_dialog(filedialog.asksaveasfilename)
