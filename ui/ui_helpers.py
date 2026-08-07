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

def scrollable_tree(
        parent: Any, *, always_y: bool=False, **tree_options: Any,
) -> tuple[tk.Frame, ttk.Treeview]:
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
    yscroll_class = ttk.Scrollbar if always_y else AutoScrollbar
    yscroll = yscroll_class(frame, orient='vertical', command=tree.yview)
    xscroll = AutoScrollbar(frame, orient='horizontal', command=tree.xview)
    tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    tree.grid(row=0, column=0, sticky='nsew')
    yscroll.grid(row=0, column=1, sticky='ns')
    xscroll.grid(row=1, column=0, sticky='ew')
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    # 初期配置後の実表示幅で再判定し、不要な横スクロールバーを残さない。
    tree.after_idle(lambda: xscroll.set(*tree.xview()))
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


def tree_toggle_all_button(
        parent: Any,
        tree: ttk.Treeview,
        **button_options: Any,
) -> ttk.Button:
    """全階層の展開状態に応じて表示が切り替わる共通ボタンを作成する。"""
    # 表示文言が切り替わっても周囲のレイアウト幅を変えない。
    button_options.setdefault('width', 14)
    button = ttk.Button(parent, **button_options)

    def expandable_items() -> list[str]:
        items: list[str] = []

        def collect(parent_item: str) -> None:
            for item in tree.get_children(parent_item):
                if tree.get_children(item):
                    items.append(item)
                    collect(item)

        collect('')
        return items

    def refresh(_event: object=None) -> None:
        items = expandable_items()
        has_closed = any(not bool(tree.item(item, 'open')) for item in items)
        button.configure(
            text='msg.0580' if has_closed else 'msg.0581',
            state='normal' if items else 'disabled',
        )

    def toggle() -> None:
        items = expandable_items()
        open_items = any(not bool(tree.item(item, 'open')) for item in items)
        for item in items:
            tree.item(item, open=open_items)
        refresh()

    button.configure(command=toggle)
    tree.bind('<<TreeviewOpen>>', lambda _event: tree.after_idle(refresh), add='+')
    tree.bind('<<TreeviewClose>>', lambda _event: tree.after_idle(refresh), add='+')
    tree.bind('<<TreeviewSelect>>', lambda _event: tree.after_idle(refresh), add='+')
    tree.after_idle(refresh)
    return button
