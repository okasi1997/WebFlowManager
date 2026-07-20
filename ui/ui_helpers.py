"""複数画面で共有する小さな ttk レイアウト部品を定義する。"""
from __future__ import annotations
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

def scrollable_tree(parent: Any, **tree_options: Any) -> tuple[ttk.Frame, ttk.Treeview]:
    """縦横スクロール対応の Treeview と、その外枠をまとめて作成する。"""
    # 各画面で grid 設定を重複させず、スクロール挙動を統一する。
    frame = ttk.Frame(parent)
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
