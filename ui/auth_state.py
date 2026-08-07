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
from browser.profile_runtime import clear_profile, persistent_profile_dir
from i18n import tr

DEFAULT_PROFILE = 'default'
NO_PROFILE = 'none'
GENERATED_GROUP_STATE_SUFFIX = re.compile(r'_group_[A-Za-z0-9_-]+$')


def profile_path(project_dir: Path, profile: str) -> Path | None:
    if profile == NO_PROFILE:
        return None
    if profile == DEFAULT_PROFILE:
        return project_dir / 'data' / 'browser_state.json'
    return project_dir / 'data' / 'browser_states' / f'{profile}.json'


class AuthStateDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, project_dir: Path, session: AuthBrowserSession,
                 current_profile: str, save_profile: Callable[[str], None], start_url: str,
                 embedded: bool=False) -> None:
        self.embedded = embedded
        if embedded:
            ttk.Frame.__init__(self, parent, style='Page.TFrame')
        else:
            super().__init__(parent)
        self.project_dir, self.session = project_dir, session
        self.save_profile, self.start_url = save_profile, start_url
        if not embedded:
            self.title('msg.0460')
            self.geometry('680x390')
            self.minsize(580, 340)
        self.profile = tk.StringVar(value=current_profile)
        self._refreshing_profiles = False
        self.url = tk.StringVar(value=start_url)
        self.status_text = tk.StringVar(value=tr('msg.0469'))
        body = ttk.Frame(
            self,
            padding=(30, 22) if embedded else (24, 20),
            style='Page.TFrame' if embedded else 'TFrame',
        )
        if embedded:
            self.columnconfigure(0, weight=1)
            self.rowconfigure(0, weight=1)
            body.grid(row=0, column=0, sticky='nsew')
        else:
            body.pack(fill='x', expand=False)
        label_style = 'EmbeddedCard.TLabel' if embedded else 'TLabel'
        subtle_style = 'EmbeddedCardSubtle.TLabel' if embedded else 'Subtle.TLabel'
        frame_style = 'EmbeddedCardBody.TFrame' if embedded else 'TFrame'
        body.columnconfigure(
            0,
            weight=2 if embedded else 1,
            uniform='auth_panel' if embedded else None,
        )
        if embedded:
            # 各パネルの要求幅に左右されず、左右の比率を 2:3 に保つ。
            # 特に長い「プロファイルなし」の説明へ切り替えた際も、
            # 左側のプロファイル一覧幅を変化させない。
            body.columnconfigure(1, weight=3, uniform='auth_panel')
            body.rowconfigure(0, weight=1)

        profile_panel = ttk.Frame(
            body,
            padding=(28, 24) if embedded else 0,
            style='EmbeddedCard.TFrame' if embedded else frame_style,
        )
        profile_panel.grid(
            row=0, column=0, sticky='nsew',
            padx=(0, 10) if embedded else 0,
        )
        profile_panel.columnconfigure(0, weight=1)
        ttk.Label(profile_panel, text='msg.0514', style=label_style).grid(
            row=0, column=0, columnspan=4, pady=(0, 8), sticky='w'
        )
        if embedded:
            profile_panel.rowconfigure(1, weight=1)
            profile_list_frame = ttk.Frame(profile_panel, style=frame_style)
            profile_list_frame.grid(
                row=1, column=0, columnspan=4, sticky='nsew', pady=(4, 12),
            )
            self.profile_list = ttk.Treeview(
                profile_list_frame, columns=('status',), show='tree headings',
                selectmode='browse', style='Status.Treeview',
            )
            self.profile_list.heading('#0', text='msg.0461')
            self.profile_list.heading('status', text='msg.0520')
            self.profile_list.column('#0', width=220, minwidth=140, stretch=True)
            self.profile_list.column('status', width=100, minwidth=80, anchor='center', stretch=False)
            self.profile_list.grid(row=0, column=0, sticky='nsew')
            profile_list_frame.rowconfigure(0, weight=1)
            profile_list_frame.columnconfigure(0, weight=1)
            profile_actions = ttk.Frame(profile_panel, style=frame_style)
            profile_actions.grid(row=2, column=0, columnspan=4, sticky='ew')
            profile_actions.columnconfigure(0, weight=1)
            profile_actions.columnconfigure(1, weight=1)
            ttk.Button(
                profile_actions, text='msg.0462', command=self._new_profile,
                style='Secondary.TButton',
            ).grid(row=0, column=0, padx=(0, 4), sticky='ew')
            ttk.Button(
                profile_actions, text='msg.0463', command=self._delete_profile,
                style='Danger.TButton',
            ).grid(row=0, column=1, padx=(4, 0), sticky='ew')
        else:
            profile_panel.columnconfigure(1, weight=1)
            ttk.Label(profile_panel, text='msg.0461', style=label_style).grid(
                row=1, column=0, padx=(0, 10), pady=7, sticky='w'
            )
            self.profile_box = ttk.Combobox(
                profile_panel, textvariable=self.profile, state='readonly', width=25,
            )
            self.profile_box.grid(row=1, column=1, pady=6, sticky='w')
            ttk.Button(profile_panel, text='msg.0462', command=self._new_profile, style='Secondary.TButton').grid(row=1, column=2, padx=(8, 0))
            ttk.Button(profile_panel, text='msg.0463', command=self._delete_profile, style='Danger.TButton').grid(row=1, column=3, padx=(6, 0))
            ttk.Label(profile_panel, text='URL', style=label_style).grid(row=2, column=0, padx=(0, 10), pady=7, sticky='w')
            ttk.Entry(profile_panel, textvariable=self.url, style='Dialog.TEntry').grid(row=2, column=1, columnspan=3, pady=6, sticky='ew')
            self.path_label = ttk.Label(profile_panel, text='', style=subtle_style)
            self.path_label.grid(row=3, column=1, columnspan=3, sticky='w')

        actions = ttk.Frame(
            body,
            padding=(28, 24) if embedded else 0,
            style='EmbeddedCard.TFrame' if embedded else frame_style,
        )
        actions.grid(
            row=0 if embedded else 1,
            column=1 if embedded else 0,
            padx=(10, 0) if embedded else 0,
            pady=(0, 0) if embedded else (18, 0),
            sticky='nsew' if embedded else 'ew',
        )
        actions.columnconfigure(0, weight=0, minsize=100)
        actions.columnconfigure(1, weight=1)
        ttk.Label(actions, text='msg.0512', style=label_style).grid(
            row=0, column=0, columnspan=2, pady=(0, 8), sticky='w'
        )
        action_row = 1
        if embedded:
            ttk.Label(actions, text='msg.0461', style=label_style).grid(row=1, column=0, sticky='w', pady=6)
            ttk.Label(actions, textvariable=self.profile, style=label_style).grid(row=1, column=1, sticky='w', pady=6)
            ttk.Label(actions, text='URL', style=label_style).grid(row=2, column=0, sticky='w', pady=6)
            ttk.Entry(actions, textvariable=self.url, style='Dialog.TEntry').grid(row=2, column=1, sticky='ew', pady=6)
            self.path_label = ttk.Label(actions, text='', style=subtle_style, wraplength=460, justify='left')
            self.path_label.grid(row=3, column=0, columnspan=2, sticky='w', pady=(2, 8))
            ttk.Separator(actions).grid(row=4, column=0, columnspan=2, pady=(8, 14), sticky='ew')
            action_row = 5
        browser_actions = ttk.Frame(actions, style=frame_style)
        browser_actions.grid(row=action_row, column=0, columnspan=2, sticky='ew')
        browser_actions.columnconfigure(0, weight=1, uniform='browser_action')
        browser_actions.columnconfigure(1, weight=1, uniform='browser_action')
        ttk.Button(
            browser_actions, text='msg.0464', command=self._open,
            style='Secondary.TButton',
        ).grid(row=0, column=0, padx=(0, 4), sticky='ew')
        ttk.Button(
            browser_actions, text='msg.0467', command=self._close_browser,
            style='Secondary.TButton',
        ).grid(row=0, column=1, padx=(4, 0), sticky='ew')
        if embedded:
            ttk.Separator(actions).grid(
                row=action_row + 1, column=0, columnspan=2, pady=(22, 16), sticky='ew'
            )
            ttk.Label(actions, text='msg.0516', style=label_style).grid(
                row=action_row + 2, column=0, columnspan=2, sticky='w'
            )
            status_bar = ttk.Frame(actions, style=frame_style)
            status_bar.grid(row=action_row + 3, column=0, columnspan=2, pady=(10, 0), sticky='nsew')
            status_bar.columnconfigure(0, weight=1)
            status_bar.rowconfigure(0, weight=1)
            actions.rowconfigure(action_row + 3, weight=1)
            ttk.Label(
                status_bar,
                textvariable=self.status_text,
                wraplength=460,
                justify='left',
                style=subtle_style,
            ).grid(row=0, column=0, sticky='nw')
            ttk.Button(
                actions, text='msg.0465', command=self._save,
                style='Primary.TButton', width=20,
            ).grid(row=action_row + 4, column=1, pady=(16, 0), sticky='e')
        else:
            ttk.Button(
                actions, text='msg.0465', command=self._save,
                style='Primary.TButton',
            ).grid(row=action_row + 1, column=0, columnspan=2, pady=(8, 0), sticky='ew')
            ttk.Separator(body).grid(
                row=2, column=0, pady=(14, 12), sticky='ew',
            )
            ttk.Label(
                body, textvariable=self.status_text, wraplength=820,
                justify='left', style=label_style,
            ).grid(row=3, column=0, sticky='nw')
            footer = ttk.Frame(self, padding=(14, 10), style='TFrame')
            footer.pack(fill='x')
            ttk.Button(footer, text='msg.0144', command=self._apply, style='Primary.TButton', width=14).pack(side='right')
            ttk.Button(footer, text='msg.0145', command=self.destroy, style='Secondary.TButton', width=14).pack(side='right', padx=8)
        if embedded:
            self.profile_list.bind('<<TreeviewSelect>>', self._profile_list_selected)
        else:
            self.profile_box.bind('<<ComboboxSelected>>', lambda _event: self._selection_changed())
        self._refresh_profiles(current_profile)
        if not embedded:
            self.transient(parent)
            self.grab_set()

    def _profiles(self) -> list[str]:
        folder = self.project_dir / 'data' / 'browser_states'
        custom = sorted(
            (
                path.stem
                for path in folder.glob('*.json')
                if not GENERATED_GROUP_STATE_SUFFIX.search(path.stem)
            ),
            key=str.casefold,
        ) if folder.exists() else []
        return [DEFAULT_PROFILE, NO_PROFILE, *custom]

    def _refresh_profiles(self, selected: str | None=None, apply: bool=False) -> None:
        profiles = self._profiles()
        selected_profile = selected if selected in profiles else DEFAULT_PROFILE
        self.profile.set(selected_profile)
        if self.embedded:
            self._refreshing_profiles = True
            self.profile_list.delete(*self.profile_list.get_children())
            for profile in profiles:
                self.profile_list.insert(
                    '', 'end', iid=profile, text=profile,
                    values=(tr('msg.0521') if profile == selected_profile else '',),
                )
            self.profile_list.selection_set(selected_profile)
            self.profile_list.focus(selected_profile)
            self.profile_list.see(selected_profile)
            self.after_idle(lambda: setattr(self, '_refreshing_profiles', False))
        else:
            self.profile_box.configure(values=profiles)
        self._selection_changed(apply=apply)

    def _profile_list_selected(self, _event: object=None) -> None:
        if self._refreshing_profiles:
            return
        selection = self.profile_list.selection()
        if not selection:
            return
        self.profile.set(selection[0])
        for item in self.profile_list.get_children():
            self.profile_list.set(item, 'status', tr('msg.0521') if item == selection[0] else '')
        self._selection_changed(apply=True)

    def _selection_changed(self, apply: bool=False) -> None:
        path = profile_path(self.project_dir, self.profile.get())
        profile_dir = persistent_profile_dir(self.project_dir, path)
        self.path_label.configure(text='msg.0470' if profile_dir is None else str(profile_dir))
        if apply:
            self.save_profile(self.profile.get())
            self.status_text.set(tr('msg.0515'))

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
        self._refresh_profiles(name, apply=self.embedded)

    def _delete_profile(self) -> None:
        name = self.profile.get()
        if name in {DEFAULT_PROFILE, NO_PROFILE}:
            messagebox.showinfo('msg.0048', 'msg.0473', parent=self)
            return
        path = profile_path(self.project_dir, name)
        if messagebox.askyesno('msg.0046', f'msg.0474{name}?', parent=self):
            if path is not None and path.exists():
                path.unlink()
            profile_dir = persistent_profile_dir(self.project_dir, path)
            if profile_dir is not None:
                clear_profile(self.project_dir, profile_dir)
            self._refresh_profiles(DEFAULT_PROFILE, apply=self.embedded)

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

    def _close_browser(self) -> None:
        self._background(self.session.close_browser, lambda _result: 'msg.0481')

    def _apply(self) -> None:
        self.save_profile(self.profile.get())
        if self.embedded:
            self.status_text.set(tr('msg.0503'))
        else:
            self.destroy()
