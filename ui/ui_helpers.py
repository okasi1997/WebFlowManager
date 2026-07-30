"""複数画面で共有する小さな ttk レイアウト部品を定義する。"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Any
from i18n import tr

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


def ask_yes_no(parent: tk.Misc, title: str, message: str) -> bool:
    """OS の表示言語に依存しない「はい／いいえ」確認画面を表示する。"""
    dialog = tk.Toplevel(parent)
    dialog.title(tr(title))
    dialog.geometry('440x160')
    dialog.resizable(False, False)
    dialog.transient(parent)
    result = {'value': False}

    body = ttk.Frame(dialog, padding=(18, 16, 18, 10))
    body.pack(fill='both', expand=True)
    ttk.Label(body, text='?', anchor='center', font=('', 16), width=2).pack(side='left', padx=(0, 12))
    ttk.Label(body, text=tr(message), wraplength=350, justify='left').pack(side='left', fill='both', expand=True)

    footer = ttk.Frame(dialog, padding=(12, 8))
    footer.pack(fill='x')

    def finish(value: bool) -> None:
        result['value'] = value
        dialog.destroy()

    ttk.Button(footer, text='msg.0488', command=lambda: finish(False), width=10).pack(side='right')
    yes_button = ttk.Button(
        footer, text='msg.0487', command=lambda: finish(True), style='Primary.TButton', width=10)
    yes_button.pack(side='right', padx=(0, 8))
    dialog.protocol('WM_DELETE_WINDOW', lambda: finish(False))
    dialog.bind('<Escape>', lambda _event: finish(False))
    dialog.bind('<Return>', lambda _event: finish(True))
    dialog.grab_set()
    yes_button.focus_set()
    parent.wait_window(dialog)
    return result['value']
