"""ログイン状態プロファイルの管理画面。"""
from __future__ import annotations

import json
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Callable

from browser.auth_session import AuthBrowserSession
from i18n import tr

DEFAULT_PROFILE = 'default'
NO_PROFILE = 'none'


def profile_path(project_dir: Path, profile: str) -> Path | None:
    if profile == NO_PROFILE:
        return None
    if profile == DEFAULT_PROFILE:
        return project_dir / 'data' / 'browser_state.json'
    return project_dir / 'data' / 'browser_states' / f'{profile}.json'


class AuthStateDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, project_dir: Path, session: AuthBrowserSession,
                 current_profile: str, save_profile: Callable[[str], None], start_url: str) -> None:
        super().__init__(parent)
        self.project_dir, self.session = project_dir, session
        self.save_profile, self.start_url = save_profile, start_url
        self.title('msg.0460')
        self.geometry('680x390')
        self.minsize(580, 340)
        self.profile = tk.StringVar(value=current_profile)
        self.url = tk.StringVar(value=start_url)
        self.status_text = tk.StringVar(value=tr('msg.0469'))
        body = ttk.Frame(self, padding=16)
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text='msg.0461').grid(row=0, column=0, padx=(0, 10), pady=6, sticky='e')
        self.profile_box = ttk.Combobox(body, textvariable=self.profile, state='readonly')
        self.profile_box.grid(row=0, column=1, pady=6, sticky='ew')
        ttk.Button(body, text='msg.0462', command=self._new_profile).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(body, text='msg.0463', command=self._delete_profile, style='Danger.TButton').grid(row=0, column=3, padx=(6, 0))
        ttk.Label(body, text='URL').grid(row=1, column=0, padx=(0, 10), pady=6, sticky='e')
        ttk.Entry(body, textvariable=self.url, style='Dialog.TEntry').grid(row=1, column=1, columnspan=3, pady=6, sticky='ew')
        self.path_label = ttk.Label(body, text='', style='Subtle.TLabel')
        self.path_label.grid(row=2, column=1, columnspan=3, sticky='w')
        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, columnspan=4, pady=(18, 10), sticky='ew')
        for index, (text, command) in enumerate((('msg.0464', self._open), ('msg.0465', self._save), ('msg.0466', self._check), ('msg.0467', self._close_browser))):
            ttk.Button(actions, text=text, command=command, style='Action.TButton').grid(row=0, column=index, padx=4, sticky='ew')
            actions.columnconfigure(index, weight=1, uniform='auth_action')
        ttk.Separator(body).grid(row=4, column=0, columnspan=4, pady=(8, 12), sticky='ew')
        ttk.Label(body, textvariable=self.status_text, wraplength=610, justify='left').grid(row=5, column=0, columnspan=4, sticky='nw')
        footer = ttk.Frame(self, padding=(14, 10))
        footer.pack(fill='x')
        ttk.Button(footer, text='msg.0144', command=self._apply, style='Primary.TButton', width=14).pack(side='right')
        ttk.Button(footer, text='msg.0145', command=self.destroy, style='Secondary.TButton', width=14).pack(side='right', padx=8)
        self.profile_box.bind('<<ComboboxSelected>>', lambda _event: self._selection_changed())
        self._refresh_profiles(current_profile)
        self.transient(parent)
        self.grab_set()

    def _profiles(self) -> list[str]:
        folder = self.project_dir / 'data' / 'browser_states'
        custom = sorted((path.stem for path in folder.glob('*.json')), key=str.casefold) if folder.exists() else []
        return [DEFAULT_PROFILE, NO_PROFILE, *custom]

    def _refresh_profiles(self, selected: str | None=None) -> None:
        profiles = self._profiles()
        self.profile_box.configure(values=profiles)
        self.profile.set(selected if selected in profiles else DEFAULT_PROFILE)
        self._selection_changed()

    def _selection_changed(self) -> None:
        path = profile_path(self.project_dir, self.profile.get())
        self.path_label.configure(text='msg.0470' if path is None else str(path))

    def _new_profile(self) -> None:
        name = simpledialog.askstring('msg.0462', 'msg.0471', parent=self)
        if not name:
            return
        name = name.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in {DEFAULT_PROFILE, NO_PROFILE}:
            messagebox.showerror('msg.0159', 'msg.0472', parent=self)
            return
        path = profile_path(self.project_dir, name)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        self._refresh_profiles(name)

    def _delete_profile(self) -> None:
        name = self.profile.get()
        if name in {DEFAULT_PROFILE, NO_PROFILE}:
            messagebox.showinfo('msg.0048', 'msg.0473', parent=self)
            return
        path = profile_path(self.project_dir, name)
        if messagebox.askyesno('msg.0046', f'msg.0474{name}?', parent=self):
            if path is not None and path.exists():
                path.unlink()
            self._refresh_profiles(DEFAULT_PROFILE)

    def _background(self, operation: Callable[[], object], success: Callable[[object], str]) -> None:
        self.status_text.set(tr('msg.0475'))
        def worker() -> None:
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, str(exc)
            self.after(0, lambda: self.status_text.set(tr(f'msg.0476{error}' if error else success(result))))
        threading.Thread(target=worker, daemon=True).start()

    def _open(self) -> None:
        path = profile_path(self.project_dir, self.profile.get())
        url = self.url.get().strip() or self.start_url
        self._background(lambda: self.session.open(path, url), lambda _result: 'msg.0477')

    def _save(self) -> None:
        path = profile_path(self.project_dir, self.profile.get())
        if path is None:
            messagebox.showinfo('msg.0048', 'msg.0478', parent=self)
            return
        self._background(lambda: self.session.save(path), lambda result: f'msg.0479{result}')

    def _check(self) -> None:
        self._background(self.session.status, lambda result: f'msg.0480{result[0]} | {result[1]} | Cookie: {result[2]}')

    def _close_browser(self) -> None:
        self._background(self.session.close_browser, lambda _result: 'msg.0481')

    def _apply(self) -> None:
        self.save_profile(self.profile.get())
        self.destroy()
