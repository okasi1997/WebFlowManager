"""複数画面で共有する小さな ttk レイアウト部品を定義する。"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Any

class AutoScrollbar(ttk.Scrollbar):
    """全内容が表示できる場合だけ、自動的に非表示になるスクロールバー。"""

    def set(self, first: str, last: str) -> None:
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.grid_remove()
        else:
            self.grid()
        super().set(first, last)

def scrollable_tree(parent: Any, **tree_options: Any) -> tuple[tk.Frame, ttk.Treeview]:
    """縦横スクロール対応の Treeview と、その外枠をまとめて作成する。"""
    # 各画面で grid 設定を重複させず、スクロール挙動を統一する。
    # Treeview の選択背景や自動スクロールバーに上書きされない固定外枠を使う。
    # ttk のテーマ境界は Windows の表示倍率によって一部が欠ける場合がある。
    frame = tk.Frame(
        parent,
        background='#D4D4D4',
        highlightbackground='#D4D4D4',
        highlightcolor='#D4D4D4',
        highlightthickness=1,
        bd=0,
    )
    tree = ttk.Treeview(frame, **tree_options)
    yscroll = AutoScrollbar(frame, orient='vertical', command=tree.yview)
    xscroll = AutoScrollbar(frame, orient='horizontal', command=tree.xview)
    tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    tree.grid(row=0, column=0, sticky='nsew')
    yscroll.grid(row=0, column=1, sticky='ns')
    xscroll.grid(row=1, column=0, sticky='ew')
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return (frame, tree)


def toggle_tree_indicator_on_double_click(
        tree: ttk.Treeview,
        event: tk.Event,
        item: str,
) -> bool:
    """矢印の高速連続クリックで欠ける2回目の展開・折りたたみを補完する。"""
    if tree.identify_element(event.x, event.y) != 'Treeitem.indicator':
        return False
    if not tree.get_children(item):
        return False
    opened = bool(tree.item(item, 'open'))
    tree.item(item, open=not opened)
    return True
