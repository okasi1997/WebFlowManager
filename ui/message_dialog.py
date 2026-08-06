"""アプリ共通の字体と配色を使用するメッセージダイアログを提供する。"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from i18n import tr


class AppMessageDialog(tk.Toplevel):
    """OS 固有の字体に依存しない、アプリ共通のメッセージダイアログ。"""

    def __init__(
            self,
            parent: tk.Misc,
            title: str,
            message: str,
            kind: str,
            confirm: bool,
            cancelable: bool=False,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result: bool | None = None if cancelable else False
        self.cancelable = cancelable
        self.protocol('WM_DELETE_WINDOW', self._close_window)
        self.bind('<Escape>', lambda _event: self._close_window())

        body = ttk.Frame(self, padding=(30, 26, 30, 24), style='DialogCardBody.TFrame')
        body.pack(fill='both', expand=True)
        icon_style = {
            'error': 'ErrorIcon.TLabel',
            'warning': 'WarningIcon.TLabel',
        }.get(kind, 'InfoIcon.TLabel')
        icon_text = {'error': '!', 'warning': '!', 'question': '?', 'info': 'i'}.get(kind, 'i')
        ttk.Label(body, text=icon_text, style=icon_style, anchor='center', width=3).grid(
            row=0,
            column=0,
            sticky='w',
            padx=(0, 22),
        )
        ttk.Label(
            body,
            text=message,
            style='MessageBody.TLabel',
            justify='left',
            wraplength=500,
        ).grid(row=0, column=1, sticky='w')
        body.columnconfigure(1, weight=1)

        footer = ttk.Frame(self, padding=(26, 10), style='DialogFooter.TFrame')
        footer.pack(fill='x')
        buttons = ttk.Frame(footer, style='DialogFooter.TFrame')
        buttons.pack(side='right')
        if confirm:
            ttk.Button(
                buttons,
                text='msg.0037',
                command=self._accept,
                style='MessagePrimary.TButton',
            ).grid(row=0, column=0, sticky='nsew', padx=(0, 10))
            ttk.Button(
                buttons,
                text='msg.0038',
                command=self._reject,
                style='MessageSecondary.TButton',
            ).grid(row=0, column=1, sticky='nsew')
            if cancelable:
                ttk.Button(
                    buttons,
                    text='msg.0145',
                    command=self._close_window,
                    style='MessageSecondary.TButton',
                ).grid(row=0, column=2, sticky='nsew', padx=(10, 0))
            buttons.columnconfigure(0, minsize=112, uniform='message_action')
            buttons.columnconfigure(1, minsize=112, uniform='message_action')
            if cancelable:
                buttons.columnconfigure(2, minsize=112, uniform='message_action')
            buttons.rowconfigure(0, minsize=34)
        else:
            ttk.Button(
                buttons,
                text='OK',
                command=self._accept,
                style='MessagePrimary.TButton',
            ).grid(row=0, column=0, sticky='nsew')
            buttons.columnconfigure(0, minsize=112)
            buttons.rowconfigure(0, minsize=34)

        self.update_idletasks()
        width = max(480, self.winfo_reqwidth())
        height = self.winfo_reqheight()
        anchor = parent.winfo_toplevel()
        anchor.update_idletasks()
        x = anchor.winfo_rootx() + max(0, (anchor.winfo_width() - width) // 2)
        y = anchor.winfo_rooty() + max(0, (anchor.winfo_height() - height) // 2)
        self.geometry(f'{width}x{height}+{x}+{y}')
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_force()

    def _accept(self) -> None:
        self.result = True
        self.destroy()

    def _reject(self) -> None:
        self.result = False
        self.destroy()

    def _close_window(self) -> None:
        self.result = None if self.cancelable else False
        self.destroy()


def install_app_messageboxes(root: tk.Misc) -> None:
    """messagebox の公開関数をアプリ共通ダイアログへ差し替える。"""
    if getattr(messagebox, '_flow_app_dialogs_installed', False):
        return
    messagebox._flow_app_dialogs_installed = True

    def show_dialog(
            title: Any = '',
            message: Any = '',
            *,
            parent: tk.Misc | None = None,
            kind: str = 'info',
            confirm: bool = False,
            cancelable: bool = False,
            **_options: Any,
    ) -> bool | None:
        owner = parent or root
        dialog = AppMessageDialog(
            owner, str(tr(title)), str(tr(message)), kind, confirm, cancelable,
        )
        dialog.wait_window()
        return dialog.result

    messagebox.askyesno = lambda title=None, message=None, **options: show_dialog(
        title,
        message,
        kind='question',
        confirm=True,
        **options,
    )
    messagebox.askyesnocancel = lambda title=None, message=None, **options: show_dialog(
        title,
        message,
        kind='question',
        confirm=True,
        cancelable=True,
        **options,
    )
    messagebox.showinfo = lambda title=None, message=None, **options: show_dialog(
        title,
        message,
        kind='info',
        **options,
    )
    messagebox.showwarning = lambda title=None, message=None, **options: show_dialog(
        title,
        message,
        kind='warning',
        **options,
    )
    messagebox.showerror = lambda title=None, message=None, **options: show_dialog(
        title,
        message,
        kind='error',
        **options,
    )
