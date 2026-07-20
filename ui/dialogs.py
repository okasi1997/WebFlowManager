"""イベント編集と実行時変数入力のダイアログを定義する。"""
from __future__ import annotations
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable
from core.conditions import OPERATORS, decode_guard, summarize_guard
from i18n import tr
from ui.ui_helpers import AutoScrollbar


class RoundedCard(tk.Canvas):
    """Canvas-backed card with a real rounded outline and a ttk content frame."""

    def __init__(self, parent: tk.Misc, radius: int=10, padding: tuple[int, int]=(12, 9)) -> None:
        super().__init__(parent, height=1, bg='#F3F3F3', highlightthickness=0, bd=0)
        self.radius = radius
        self.card_padding = padding
        self.stretch_height = False
        self.shape = self.create_polygon(0, 0, fill='#FFFFFF', outline='#DEDEDE', width=1, smooth=True, splinesteps=32)
        self.content = ttk.Frame(self, style='DialogCardBody.TFrame')
        self.content_window = self.create_window(padding[0], padding[1], anchor='nw', window=self.content)
        self.bind('<Configure>', self._resize_card)
        self.content.bind('<Configure>', self._resize_to_content)
        self.after_idle(self._sync_requested_height)

    def set_stretch(self, enabled: bool=True) -> None:
        self.stretch_height = enabled

    def _resize_to_content(self, event: tk.Event) -> None:
        self.after_idle(self._sync_requested_height)

    def _sync_requested_height(self) -> None:
        if not self.winfo_exists() or self.stretch_height:
            return
        requested = self.content.winfo_reqheight() + self.card_padding[1] * 2
        if requested > 2 and int(float(self.cget('height'))) != requested:
            self.configure(height=requested)

    def _resize_card(self, event: tk.Event) -> None:
        width = max(2, event.width)
        height = max(2, event.height)
        radius = min(self.radius, width // 3, height // 3)
        points = (
            radius, 1, width - radius, 1, width - 1, 1,
            width - 1, radius, width - 1, height - radius, width - 1, height - 1,
            width - radius, height - 1, radius, height - 1, 1, height - 1,
            1, height - radius, 1, radius, 1, 1,
        )
        self.coords(self.shape, *points)
        self.itemconfigure(self.content_window, width=max(1, width - self.card_padding[0] * 2))
        self.after_idle(self._sync_requested_height)


def guard_operator_labels() -> dict[str, str]:
    return {operator: tr(f'msg.{381 + index:04d}') for index, operator in enumerate(OPERATORS)}


class GuardRuleDialog(tk.Toplevel):
    """一つのデータ条件を、パス、演算子、比較値として編集する。"""

    def __init__(self, parent: tk.Misc, choose_path: Callable[[], str | None], rule: dict[str, str] | None=None) -> None:
        super().__init__(parent)
        self.title('msg.0393')
        self.geometry('560x260')
        self.resizable(False, False)
        self.result: dict[str, str] | None = None
        rule = rule or {'path': '', 'operator': 'eq', 'value': ''}
        self.path = tk.StringVar(value=rule['path'])
        self.operator = tk.StringVar(value=rule['operator'])
        self.expected = tk.StringVar(value=rule['value'])
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        ttk.Separator(self, style='DialogFooter.TSeparator').grid(row=0, column=0, sticky='ew')
        main = ttk.Frame(self, padding=(24, 12, 32, 12))
        main.grid(row=1, column=0, sticky='nsew')
        body = ttk.Frame(main)
        body.pack(fill='x', expand=True)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, weight=0, minsize=120)
        body.columnconfigure(0, minsize=82)
        ttk.Label(body, text='msg.0440').grid(row=0, column=0, padx=(0, 12), pady=(4, 7), sticky='e')
        ttk.Entry(body, textvariable=self.path, style='Dialog.TEntry').grid(row=0, column=1, padx=(0, 10), pady=(4, 7), sticky='ew')
        ttk.Button(body, text='msg.0140', command=lambda: self.path.set(choose_path() or self.path.get()), style='DialogInline.TButton').grid(row=0, column=2, pady=(4, 7), sticky='ew')
        ttk.Label(body, text='msg.0395').grid(row=1, column=0, padx=(0, 12), pady=7, sticky='e')
        labels = guard_operator_labels()
        self.operator_display = tk.StringVar(value=labels[self.operator.get()])
        operator_box = ttk.Combobox(body, state='readonly', textvariable=self.operator_display, values=tuple(labels.values()), style='Dialog.TCombobox')
        operator_box.grid(row=1, column=1, padx=(0, 10), pady=7, sticky='ew')
        operator_box.bind('<<ComboboxSelected>>', lambda _event: self._operator_changed(labels))
        ttk.Label(body, text='msg.0396').grid(row=2, column=0, padx=(0, 12), pady=(7, 4), sticky='e')
        self.expected_entry = ttk.Entry(body, textvariable=self.expected, style='Dialog.TEntry')
        self.expected_entry.grid(row=2, column=1, padx=(0, 10), pady=(7, 4), sticky='ew')
        ttk.Separator(self, style='DialogFooter.TSeparator').grid(row=2, column=0, sticky='ew')
        buttons = ttk.Frame(self, style='DialogFooter.TFrame', padding=(14, 10))
        buttons.grid(row=3, column=0, sticky='ew')
        button_group = ttk.Frame(buttons, style='DialogFooter.TFrame')
        button_group.pack(side='right')
        ttk.Button(button_group, text='msg.0144', command=self._save, style='Primary.TButton', width=14).grid(row=0, column=0, padx=(0, 5), sticky='ew')
        ttk.Button(button_group, text='msg.0145', command=self.destroy, style='Secondary.TButton', width=14).grid(row=0, column=1, padx=(5, 0), sticky='ew')
        button_group.columnconfigure(0, weight=1, uniform='rule_footer_action')
        button_group.columnconfigure(1, weight=1, uniform='rule_footer_action')
        self._update_expected_state()
        self.transient(parent)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self.destroy)

    def _operator_changed(self, labels: dict[str, str]) -> None:
        self.operator.set(next(key for key, label in labels.items() if label == self.operator_display.get()))
        self._update_expected_state()

    def _update_expected_state(self) -> None:
        self.expected_entry.configure(state='disabled' if self.operator.get() in {'empty', 'not_empty', 'true', 'false'} else 'normal')

    def _save(self) -> None:
        path = self.path.get().strip()
        if not path:
            messagebox.showerror('msg.0159', 'msg.0397', parent=self)
            return
        self.result = {'path': path, 'operator': self.operator.get(), 'value': self.expected.get()}
        self.destroy()


class GuardConditionDialog(tk.Toplevel):
    """AND/OR で結合したガード条件を一覧形式で編集する。"""

    def __init__(self, parent: tk.Misc, guard: Any, choose_path: Callable[[], str | None]) -> None:
        super().__init__(parent)
        self.title('msg.0398')
        self.geometry('760x430')
        self.result: dict[str, Any] | None = None
        normalized = decode_guard(guard)
        self.rules = [dict(rule) for rule in normalized['rules']]
        self.logic = tk.StringVar(value=normalized['logic'])
        header = ttk.Frame(self, padding=(14, 12))
        header.pack(fill='x')
        ttk.Label(header, text='msg.0399').pack(side='left')
        logic_labels = {'all': tr('msg.0400'), 'any': tr('msg.0401')}
        self.logic_display = tk.StringVar(value=logic_labels[self.logic.get()])
        logic_box = ttk.Combobox(header, state='readonly', width=18, textvariable=self.logic_display, values=tuple(logic_labels.values()))
        logic_box.pack(side='left', padx=8)
        logic_box.bind('<<ComboboxSelected>>', lambda _event: self.logic.set(next(key for key, label in logic_labels.items() if label == self.logic_display.get())))
        self.tree = ttk.Treeview(self, columns=('path', 'operator', 'value'), show='headings', height=9)
        self.tree.heading('path', text='msg.0394')
        self.tree.heading('operator', text='msg.0395')
        self.tree.heading('value', text='msg.0396')
        self.tree.column('path', width=280)
        self.tree.column('operator', width=170)
        self.tree.column('value', width=220)
        self.tree.pack(fill='both', expand=True, padx=14)
        self.tree.bind('<Double-1>', lambda _event: self._edit_rule(choose_path))
        actions = ttk.Frame(self, padding=(14, 8))
        actions.pack(fill='x')
        ttk.Button(actions, text='msg.0402', command=lambda: self._add_rule(choose_path)).pack(side='left', padx=(0, 4))
        ttk.Button(actions, text='msg.0035', command=lambda: self._edit_rule(choose_path)).pack(side='left', padx=4)
        ttk.Button(actions, text='msg.0010', command=self._delete_rule, style='Danger.TButton').pack(side='left', padx=4)
        ttk.Button(actions, text='msg.0145', command=self.destroy, style='Secondary.TButton').pack(side='right')
        ttk.Button(actions, text='msg.0144', command=self._save, style='Primary.TButton').pack(side='right', padx=8)
        self._refresh()
        self.transient(parent)
        self.grab_set()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        labels = guard_operator_labels()
        for index, rule in enumerate(self.rules):
            self.tree.insert('', 'end', iid=str(index), values=(rule['path'], labels[rule['operator']], rule['value']))

    def _add_rule(self, choose_path: Callable[[], str | None]) -> None:
        dialog = GuardRuleDialog(self, choose_path)
        self.wait_window(dialog)
        self._restore_modal_state()
        if dialog.result:
            self.rules.append(dialog.result)
            self._refresh()

    def _edit_rule(self, choose_path: Callable[[], str | None]) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        dialog = GuardRuleDialog(self, choose_path, self.rules[index])
        self.wait_window(dialog)
        self._restore_modal_state()
        if dialog.result:
            self.rules[index] = dialog.result
            self._refresh()

    def _restore_modal_state(self) -> None:
        # 子ダイアログを閉じた後、条件一覧へ入力フォーカスと grab を確実に戻す。
        if self.winfo_exists():
            self.grab_set()
            self.lift()
            self.focus_force()

    def _delete_rule(self) -> None:
        selection = self.tree.selection()
        if selection:
            del self.rules[int(selection[0])]
            self._refresh()

    def _save(self) -> None:
        self.result = {'logic': self.logic.get(), 'rules': self.rules}
        self.destroy()

class EventGroupDialog(tk.Toplevel):
    """ループと再試行の境界イベントを一つのグループ設定として編集する。"""

    def __init__(self, parent: tk.Misc, choose_data_path: Callable[[str], str | None], event: dict[str, Any] | None=None) -> None:
        super().__init__(parent)
        self.title('msg.0408')
        self.geometry('700x380')
        self.minsize(620, 360)
        self.resizable(True, True)
        self.result: dict[str, Any] | None = None
        self.choose_data_path = choose_data_path
        event = event or {}
        self.guard = decode_guard(event.get('guard', event.get('guard_json', '')))
        action = str(event.get('action', 'group_start'))
        self.loop_enabled = tk.BooleanVar(value=bool(event.get('loop_enabled', action == 'loop_start' or bool(event.get('data_path')))))
        self.retry_enabled = tk.BooleanVar(value=bool(event.get('retry_enabled', action == 'retry_start' or str(event.get('value', '')).strip())))
        self.name = tk.StringVar(value=str(event.get('name', '')))
        self.data_path = tk.StringVar(value=str(event.get('data_path', '')))
        self.retry_count = tk.StringVar(value=str(event.get('value', '3') or '3'))
        self.enabled = tk.BooleanVar(value=bool(event.get('enabled', 1)))

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        main = ttk.Frame(self, padding=(16, 16))
        main.grid(row=0, column=0, sticky='nsew')

        def add_card(title: str) -> ttk.Frame:
            card = RoundedCard(main)
            card.pack(fill='x', pady=(0, 10))
            section = card.content
            ttk.Label(section, text=title, style='DialogCardSection.TLabel').pack(anchor='w')
            ttk.Separator(section, style='DialogCard.TSeparator').pack(fill='x', pady=(0, 6))
            card_body = ttk.Frame(section, style='DialogCardBody.TFrame')
            card_body.pack(fill='x')
            return card_body

        body = add_card('msg.0366')
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=0, minsize=120)
        ttk.Label(body, text='msg.0409', style='DialogCard.TLabel').grid(row=0, column=0, padx=(8, 10), pady=7, sticky='e')
        ttk.Entry(body, textvariable=self.name, style='Dialog.TEntry').grid(row=0, column=1, columnspan=2, padx=(0, 8), pady=7, sticky='ew')
        ttk.Checkbutton(body, text='msg.0006', variable=self.enabled, style='DialogCard.TCheckbutton').grid(row=0, column=3, padx=(8, 8), pady=7, sticky='w')
        ttk.Label(body, text='msg.0410', style='DialogCard.TLabel').grid(row=1, column=0, padx=(8, 10), pady=7, sticky='e')
        mode_row = ttk.Frame(body, style='DialogCardBody.TFrame')
        mode_row.grid(row=1, column=1, columnspan=3, padx=(0, 8), pady=7, sticky='w')
        ttk.Checkbutton(mode_row, text='msg.0416', variable=self.loop_enabled, command=self._update_fields, style='DialogCard.TCheckbutton').pack(side='left', padx=(0, 24))
        ttk.Checkbutton(mode_row, text='msg.0417', variable=self.retry_enabled, command=self._update_fields, style='DialogCard.TCheckbutton').pack(side='left')
        self.path_label = ttk.Label(body, text='msg.0033', style='DialogCard.TLabel')
        self.path_label.grid(row=2, column=0, padx=(8, 10), pady=7, sticky='e')
        self.path_entry = ttk.Entry(body, textvariable=self.data_path, state='readonly', style='Dialog.TEntry')
        self.path_entry.grid(row=2, column=1, columnspan=2, padx=(0, 8), pady=7, sticky='ew')
        self.path_button = ttk.Button(body, text='msg.0140', command=self._choose_path, style='DialogInline.TButton')
        self.path_button.grid(row=2, column=3, padx=(8, 8), pady=7, sticky='ew')
        self.retry_label = ttk.Label(body, text='msg.0411', style='DialogCard.TLabel')
        self.retry_label.grid(row=3, column=0, padx=(8, 10), pady=7, sticky='e')
        self.retry_entry = ttk.Entry(body, textvariable=self.retry_count, style='Dialog.TEntry')
        self.retry_entry.grid(row=3, column=1, columnspan=2, padx=(0, 8), pady=7, sticky='ew')
        ttk.Label(body, text='msg.0405', style='DialogCard.TLabel').grid(row=4, column=0, padx=(8, 10), pady=7, sticky='e')
        self.guard_summary = ttk.Label(body, text=self._guard_summary(), style='DialogCardSubtle.TLabel')
        self.guard_summary.grid(row=4, column=1, columnspan=2, padx=(0, 8), pady=7, sticky='w')
        ttk.Button(body, text='msg.0403', command=self._edit_guard, style='DialogInline.TButton').grid(row=4, column=3, padx=(8, 8), pady=7, sticky='ew')

        ttk.Separator(self, style='DialogFooter.TSeparator').grid(row=1, column=0, sticky='ew')
        buttons = ttk.Frame(self, style='DialogFooter.TFrame', padding=(14, 14))
        buttons.grid(row=2, column=0, sticky='ew')
        button_group = ttk.Frame(buttons, style='DialogFooter.TFrame')
        button_group.pack(side='right')
        ttk.Button(button_group, text='msg.0144', command=self._save, style='Primary.TButton', width=16).grid(row=0, column=0, padx=(0, 5), sticky='ew')
        ttk.Button(button_group, text='msg.0145', command=self.destroy, style='Secondary.TButton', width=16).grid(row=0, column=1, padx=(5, 0), sticky='ew')
        button_group.columnconfigure(0, weight=1, uniform='group_footer_action')
        button_group.columnconfigure(1, weight=1, uniform='group_footer_action')
        self._update_fields()
        self.transient(parent)
        self.grab_set()

    def _guard_summary(self) -> str:
        return summarize_guard(self.guard, guard_operator_labels()) or tr('msg.0404')

    def _update_fields(self, _event: object=None) -> None:
        loop = self.loop_enabled.get()
        retry = self.retry_enabled.get()
        self.path_entry.configure(state='readonly' if loop else 'disabled')
        self.path_button.configure(state='normal' if loop else 'disabled')
        self.retry_entry.configure(state='normal' if retry else 'disabled')
        self.path_label.state(['!disabled'] if loop else ['disabled'])
        self.retry_label.state(['!disabled'] if retry else ['disabled'])

    def _choose_path(self) -> None:
        path = self.choose_data_path('loop_start')
        if path:
            self.data_path.set(path)

    def _edit_guard(self) -> None:
        dialog = GuardConditionDialog(self, self.guard, lambda: self.choose_data_path('condition'))
        self.wait_window(dialog)
        if self.winfo_exists():
            self.grab_set()
            self.lift()
            self.focus_force()
        if dialog.result is not None:
            self.guard = dialog.result
            self.guard_summary.configure(text=self._guard_summary())

    def _save(self) -> None:
        name = self.name.get().strip()
        if not name:
            messagebox.showerror('msg.0159', 'msg.0160', parent=self)
            return
        is_loop = self.loop_enabled.get()
        is_retry = self.retry_enabled.get()
        if is_loop and not self.data_path.get().strip():
            messagebox.showerror('msg.0159', 'msg.0412', parent=self)
            return
        if is_retry:
            try:
                if int(self.retry_count.get()) < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror('msg.0159', 'msg.0162', parent=self)
                return
        action = 'group_start'
        self.result = {'name': name, 'action': action, 'selector_type': 'none', 'selector': '',
                       'fallback_selector_type': 'none', 'fallback_selector': '',
                       'value': self.retry_count.get() if is_retry else '',
                       'timeout_ms': 10000, 'enabled': int(self.enabled.get()),
                       'continue_on_error': 0,
                       'data_path': self.data_path.get().strip() if is_loop else '',
                       'loop_enabled': int(is_loop), 'retry_enabled': int(is_retry),
                       'guard': self.guard}
        self.destroy()


class EventDialog(tk.Toplevel):
    """操作種別に応じて入力可能な項目を切り替えるイベント編集画面。"""

    def __init__(self, parent: tk.Misc, actions: tuple[str, ...], selector_types: tuple[str, ...], pick_element: Callable[..., None], test_element: Callable[..., None], open_debug: Callable[..., None], close_debug: Callable[..., None], execute_to_event: Callable[..., None] | None, choose_data_path: Callable[[str], str | None], default_url: str, event: dict[str, Any] | None=None) -> None:
        super().__init__(parent)
        self.title('msg.0131' if event else 'msg.0034')
        self.geometry('1000x660')
        self.minsize(900, 620)
        self.resizable(True, True)
        self.result: dict[str, Any] | None = None
        self.pick_element = pick_element
        self.test_element = test_element
        self.open_debug = open_debug
        self.close_debug = close_debug
        self.execute_to_event = execute_to_event
        self.choose_data_path = choose_data_path
        self.default_url = default_url
        self.field_widgets: dict[str, tk.Widget] = {}
        self.field_labels: dict[str, ttk.Label] = {}
        event = event or {}
        self.guard = decode_guard(event.get('guard', event.get('guard_json', '')))
        self.failure_action_labels = {'stop': tr('msg.0432'), 'continue': tr('msg.0433'), 'refresh': tr('msg.0425'), 'goto': tr('msg.0426')}
        stored_failure_action = str(event.get('failure_action', 'none'))
        failure_key = 'continue' if stored_failure_action == 'none' and event.get('continue_on_error', 0) else ('stop' if stored_failure_action == 'none' else stored_failure_action)
        self.values = {'name': tk.StringVar(value=str(event.get('name', ''))), 'action': tk.StringVar(value=str(event.get('action', 'click' if 'click' in actions else actions[0]))), 'selector_type': tk.StringVar(value=str(event.get('selector_type', 'role' if 'role' in selector_types else selector_types[0]))), 'selector': tk.StringVar(value=str(event.get('selector', ''))), 'fallback_selector_type': tk.StringVar(value=str(event.get('fallback_selector_type', 'none'))), 'fallback_selector': tk.StringVar(value=str(event.get('fallback_selector', ''))), 'value': tk.StringVar(value=str(event.get('value', ''))), 'timeout_ms': tk.StringVar(value=str(event.get('timeout_ms', 10000))), 'enabled': tk.BooleanVar(value=bool(event.get('enabled', 1))), 'failure_action': tk.StringVar(value=self.failure_action_labels.get(failure_key, self.failure_action_labels['stop'])), 'failure_target': tk.StringVar(value=str(event.get('failure_target', ''))), 'data_path': tk.StringVar(value=str(event.get('data_path', ''))), 'target_url': tk.StringVar(value=default_url)}

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        viewport = ttk.Frame(self)
        viewport.grid(row=0, column=0, sticky='nsew')
        viewport.rowconfigure(0, weight=1)
        viewport.columnconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, highlightthickness=0, bg='#F3F3F3')
        scrollbar_gutter = ttk.Frame(viewport, width=14)
        scrollbar_gutter.grid(row=0, column=1, sticky='ns')
        scrollbar_gutter.grid_propagate(False)
        scrollbar_gutter.rowconfigure(0, weight=1)
        content_scrollbar = AutoScrollbar(scrollbar_gutter, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=content_scrollbar.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        content_scrollbar.grid(row=0, column=0, sticky='ns')
        content = ttk.Frame(canvas, padding=(16, 16))
        content_window = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(content_window, width=event.width, height=event.height))

        def scroll_content(event: tk.Event) -> str:
            canvas.yview_scroll(int(-event.delta / 120), 'units')
            return 'break'

        canvas.bind('<MouseWheel>', scroll_content)
        content.bind('<MouseWheel>', scroll_content)
        self.bind('<MouseWheel>', scroll_content, add='+')

        columns = ttk.Frame(content)
        columns.pack(fill='both', expand=True)
        columns.columnconfigure(0, weight=5, uniform='event_dialog_column')
        columns.columnconfigure(1, weight=3, uniform='event_dialog_column')
        columns.rowconfigure(0, weight=1)
        left_column = ttk.Frame(columns)
        left_column.grid(row=0, column=0, padx=(0, 4), sticky='nsew')
        right_column = ttk.Frame(columns)
        right_column.grid(row=0, column=1, padx=(4, 0), sticky='nsew')

        def add_section(parent: ttk.Frame, title: str) -> ttk.Frame:
            # メイン画面と同じ平面背景、見出し、細い区切り線で情報のまとまりを示す。
            card = RoundedCard(parent)
            card.pack(fill='x', pady=(0, 10))
            section = card.content
            ttk.Label(section, text=title, style='DialogCardSection.TLabel').pack(anchor='w')
            ttk.Separator(section, style='DialogCard.TSeparator').pack(fill='x', pady=(0, 6))
            body = ttk.Frame(section, style='DialogCardBody.TFrame')
            body.pack(fill='x')
            body.rounded_card = card
            return body

        def add_field(section: ttk.Frame, row: int, label: str, key: str, choices: tuple[str, ...] | None=None, columnspan: int=1) -> None:
            field_label = ttk.Label(section, text=label, style='DialogCard.TLabel')
            field_label.grid(row=row, column=0, padx=(10, 8), pady=7, sticky='e')
            self.field_labels[key] = field_label
            if choices is not None:
                widget = ttk.Combobox(section, textvariable=self.values[key], values=choices, state='readonly', style='Dialog.TCombobox')
            else:
                widget = ttk.Entry(section, textvariable=self.values[key], width=48, style='Dialog.TEntry')
            widget.grid(row=row, column=1, columnspan=columnspan, padx=(0, 10), pady=7, sticky='ew')
            self.field_widgets[key] = widget
            section.columnconfigure(1, weight=1)

        basic = add_section(left_column, 'msg.0366')
        basic.columnconfigure(1, weight=1)
        basic.columnconfigure(3, weight=0, minsize=120)
        add_field(basic, 0, 'msg.0028', 'name', columnspan=2)
        self.enabled_check = ttk.Checkbutton(basic, text='msg.0006', variable=self.values['enabled'], style='DialogCard.TCheckbutton')
        self.enabled_check.grid(row=0, column=3, padx=(8, 10), pady=7, sticky='w')
        add_field(basic, 1, 'msg.0132', 'action', actions, columnspan=2)
        add_field(basic, 2, 'msg.0133', 'value', columnspan=2)
        self.file_choose_button = ttk.Button(basic, text='msg.0419', command=self._choose_upload_file, style='DialogInline.TButton')
        self.file_choose_button.grid(row=2, column=3, padx=(8, 10), pady=7, sticky='ew')
        add_field(basic, 3, 'msg.0134', 'timeout_ms', columnspan=2)
        ttk.Label(basic, text='msg.0405', style='DialogCard.TLabel').grid(row=4, column=0, padx=(10, 8), pady=7, sticky='e')
        self.guard_button = ttk.Button(basic, text='msg.0403', command=self._edit_guard, style='DialogInline.TButton')
        self.guard_button.grid(row=4, column=3, padx=(8, 10), pady=7, sticky='ew')
        self.guard_summary = ttk.Label(basic, text=self._guard_summary(), style='DialogCardSubtle.TLabel', anchor='w')
        self.guard_summary.grid(row=4, column=1, columnspan=2, padx=(0, 10), pady=7, sticky='ew')
        ttk.Label(basic, text='msg.0427', style='DialogCard.TLabel').grid(row=5, column=0, padx=(10, 8), pady=7, sticky='e')
        self.failure_action_box = ttk.Combobox(basic, textvariable=self.values['failure_action'], values=tuple(self.failure_action_labels.values()), state='readonly', width=18, style='Dialog.TCombobox')
        self.failure_action_box.grid(row=5, column=1, columnspan=2, padx=(0, 10), pady=7, sticky='ew')
        self.failure_target_label = ttk.Label(basic, text='msg.0428', style='DialogCard.TLabel')
        self.failure_target_label.grid(row=6, column=0, padx=(10, 8), pady=7, sticky='e')
        self.failure_target_entry = ttk.Entry(basic, textvariable=self.values['failure_target'], style='Dialog.TEntry')
        self.failure_target_entry.grid(row=6, column=1, columnspan=2, padx=(0, 10), pady=7, sticky='ew')
        self.failure_action_box.bind('<<ComboboxSelected>>', self._update_failure_fields)

        locator = add_section(left_column, 'msg.0367')
        locator.columnconfigure(1, weight=1)
        locator.columnconfigure(3, weight=0, minsize=120)
        add_field(locator, 0, 'msg.0030', 'selector_type', selector_types, columnspan=2)
        add_field(locator, 1, 'msg.0031', 'selector', columnspan=2)
        locator.rounded_card.set_stretch(True)
        locator.rounded_card.pack_configure(fill='both', expand=True)
        locator.pack_configure(fill='both', expand=True)

        debug = add_section(right_column, 'msg.0368')
        self.target_url_label = ttk.Label(debug, text='msg.0135', style='DialogCard.TLabel')
        self.target_url_label.grid(row=0, column=0, padx=(10, 8), pady=7, sticky='e')
        self.target_url_entry = ttk.Entry(debug, textvariable=self.values['target_url'], width=24, style='Dialog.TEntry')
        self.target_url_entry.grid(row=0, column=1, padx=(0, 10), pady=7, sticky='ew')
        debug.columnconfigure(1, weight=1)

        def make_icon(pattern: tuple[str, ...]) -> tk.PhotoImage:
            image = tk.PhotoImage(width=16, height=16)
            for y, line in enumerate(pattern):
                for x, pixel in enumerate(line):
                    if pixel == '#':
                        image.put('#333333', (x + 1, y + 1))
            return image

        icon_patterns = {
            'open': ('.........##...', '.##########...', '.#.......##...', '.#.....##.#...', '.#...##...#...', '.#..#.....#...', '.#........#...', '.#........#...', '.##########...'),
            'close': ('............', '..#......#..', '...#....#...', '....#..#....', '.....##.....', '.....##.....', '....#..#....', '...#....#...', '..#......#..'),
            'pick': ('....####....', '..##....##..', '.#...##...#.', '.#..####..#.', '#..######..#', '#..######..#', '.#..####..#.', '.#...##...#.', '..##....##..', '....####....'),
            'test': ('....####....', '..##....##..', '.#........#.', '.#..#.....#.', '.#...#....#.', '.#....#...#.', '.#..#..#..#.', '.#...##...#.', '..##....##..', '....####....'),
            'play': ('...#........', '...###......', '...#####....', '...#######..', '...########.', '...#######..', '...#####....', '...###......', '...#........'),
            'tree': ('.....##.....', '.....##.....', '..########..', '..#..##..#..', '..#..##..#..', '.###.##.###.', '.###....###.'),
            'clear': ('...######...', '....####....', '..########..', '..#.#..#.#..', '..#.#..#.#..', '..#.#..#.#..', '..#.#..#.#..', '..#......#..', '...######...'),
        }
        self._event_button_icons = {name: make_icon(pattern) for name, pattern in icon_patterns.items()}

        page_actions = add_section(right_column, 'msg.0439')
        self.open_debug_button = ttk.Button(page_actions, text='msg.0352', image=self._event_button_icons['open'], compound='left', command=self._open_debug, style='DialogAction.TButton')
        self.open_debug_button.grid(row=0, column=0, padx=(0, 4), pady=(2, 5), sticky='ew')
        self.close_debug_button = ttk.Button(page_actions, text='msg.0356', image=self._event_button_icons['close'], compound='left', command=self._close_debug, style='DialogAction.TButton')
        self.close_debug_button.grid(row=0, column=1, padx=(4, 0), pady=(2, 5), sticky='ew')
        self.pick_button = ttk.Button(page_actions, text='msg.0353', image=self._event_button_icons['pick'], compound='left', command=self._pick, style='DialogAction.TButton')
        self.pick_button.grid(row=1, column=0, padx=(0, 4), pady=5, sticky='ew')
        self.test_button = ttk.Button(page_actions, text='msg.0354', image=self._event_button_icons['test'], compound='left', command=self._test, style='DialogAction.TButton')
        self.test_button.grid(row=1, column=1, padx=(4, 0), pady=5, sticky='ew')
        self.execute_to_event_button = ttk.Button(page_actions, text='msg.0355', image=self._event_button_icons['play'], compound='left', command=self._execute_to_event, style='DialogAction.TButton')
        self.execute_to_event_button.grid(row=2, column=0, columnspan=2, pady=5, sticky='ew')
        self.execute_to_event_button.configure(state='normal' if execute_to_event else 'disabled')
        page_actions.columnconfigure(0, weight=1, uniform='debug_page_action')
        page_actions.columnconfigure(1, weight=1, uniform='debug_page_action')
        self.pick_status = ttk.Label(page_actions, text='msg.0138', style='DialogCardSubtle.TLabel')
        self.pick_status.grid(row=3, column=0, columnspan=2, pady=(10, 8), sticky='w')

        data_frame = add_section(right_column, 'msg.0369')
        self.data_path_entry = ttk.Entry(data_frame, textvariable=self.values['data_path'], width=24, state='readonly', style='Dialog.TEntry')
        self.data_path_entry.grid(row=0, column=0, columnspan=2, pady=(2, 10), sticky='ew')
        self.data_path_label = ttk.Label(data_frame, text='msg.0033', style='DialogPlaceholder.TLabel')

        def update_data_placeholder(*_args: object) -> None:
            if self.values['data_path'].get():
                self.data_path_label.place_forget()
            else:
                self.data_path_label.place(in_=self.data_path_entry, x=8, rely=0.5, anchor='w')

        self.values['data_path'].trace_add('write', update_data_placeholder)
        self.after_idle(update_data_placeholder)
        self.data_choose_button = ttk.Button(data_frame, text='msg.0140', image=self._event_button_icons['tree'], compound='left', command=self._choose_data, style='DialogAction.TButton')
        self.data_choose_button.grid(row=1, column=0, padx=(0, 4), sticky='ew')
        self.data_clear_button = ttk.Button(data_frame, text='msg.0141', image=self._event_button_icons['clear'], compound='left', command=lambda: self.values['data_path'].set(''), style='DialogAction.TButton')
        self.data_clear_button.grid(row=1, column=1, padx=(4, 0), sticky='ew')
        data_frame.columnconfigure(0, weight=1, uniform='data_action')
        data_frame.columnconfigure(1, weight=1, uniform='data_action')
        data_frame.rounded_card.set_stretch(True)
        data_frame.rounded_card.pack_configure(fill='both', expand=True)
        data_frame.pack_configure(fill='both', expand=True)

        ttk.Separator(self, style='DialogFooter.TSeparator').grid(row=1, column=0, sticky='ew')
        buttons = ttk.Frame(self, style='DialogFooter.TFrame', padding=(14, 14))
        buttons.grid(row=2, column=0, sticky='ew')
        button_group = ttk.Frame(buttons, style='DialogFooter.TFrame')
        button_group.pack(side='right')
        ttk.Button(button_group, text='msg.0144', command=self._save, style='Primary.TButton', width=16).grid(row=0, column=0, padx=(0, 5), sticky='ew')
        ttk.Button(button_group, text='msg.0145', command=self.destroy, style='Secondary.TButton', width=16).grid(row=0, column=1, padx=(5, 0), sticky='ew')
        button_group.columnconfigure(0, weight=1, uniform='dialog_footer_action')
        button_group.columnconfigure(1, weight=1, uniform='dialog_footer_action')
        self.transient(parent)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.field_widgets['action'].bind('<<ComboboxSelected>>', self._update_action_fields)
        self._update_action_fields()

    def _set_field_enabled(self, key: str, enabled: bool) -> None:
        widget = self.field_widgets[key]
        if isinstance(widget, ttk.Combobox):
            widget.configure(state='readonly' if enabled else 'disabled')
        else:
            widget.configure(state='normal' if enabled else 'disabled')
        self.field_labels[key].state(['!disabled'] if enabled else ['disabled'])

    def _update_action_fields(self, _event: object=None) -> None:
        # locator や値が不要な操作では入力欄を無効化し、誤設定を防ぐ。
        # retry/loop の旧境界イベントでは失敗時動作を設定しない。
        action = self.values['action'].get()
        locator_actions = {'click', 'fill', 'select', 'wait', 'press', 'get_text', 'upload_file'}
        value_actions = {'goto', 'fill', 'select', 'press', 'get_text', 'screenshot', 'pause', 'retry_start', 'upload_file'}
        timeout_actions = {'goto', 'click', 'fill', 'select', 'wait', 'press', 'get_text', 'pause', 'upload_file'}
        data_actions = {'fill', 'select', 'get_text', 'loop_start', 'upload_file'}
        can_locate = action in locator_actions
        self._set_field_enabled('selector_type', can_locate)
        self._set_field_enabled('selector', can_locate)
        self._set_field_enabled('value', action in value_actions)
        self._set_field_enabled('timeout_ms', action in timeout_actions)
        for widget in (self.target_url_entry, self.open_debug_button, self.pick_button, self.test_button):
            widget.configure(state='normal' if can_locate else 'disabled')
        self.target_url_label.state(['!disabled'] if can_locate else ['disabled'])
        self.pick_status.state(['!disabled'] if can_locate else ['disabled'])
        can_execute_to_event = self.execute_to_event is not None and can_locate
        self.execute_to_event_button.configure(state='normal' if can_execute_to_event else 'disabled')
        self.close_debug_button.configure(state='normal' if can_locate else 'disabled')
        can_link_data = action in data_actions
        self.data_path_entry.configure(state='readonly' if can_link_data else 'disabled')
        for widget in (self.data_choose_button, self.data_clear_button):
            widget.configure(state='normal' if can_link_data else 'disabled')
        self.data_path_label.state(['!disabled'] if can_link_data else ['disabled'])
        self.guard_button.configure(state='disabled' if action in {'loop_start', 'loop_end', 'retry_start', 'retry_end'} else 'normal')
        self.file_choose_button.configure(state='normal' if action == 'upload_file' else 'disabled')
        self._update_failure_fields()

    def _update_failure_fields(self, _event: object=None) -> None:
        structural = self.values['action'].get() in {'loop_start', 'loop_end', 'retry_start', 'retry_end'}
        self.failure_action_box.configure(state='disabled' if structural else 'readonly')
        goto_label = self.failure_action_labels['goto']
        show_target = not structural and self.values['failure_action'].get() == goto_label
        if show_target:
            self.failure_target_label.grid()
            self.failure_target_entry.grid()
            self.failure_target_entry.configure(state='normal')
        else:
            self.failure_target_label.grid_remove()
            self.failure_target_entry.grid_remove()

    def _choose_upload_file(self) -> None:
        path = filedialog.askopenfilename(parent=self)
        if path:
            self.values['value'].set(path)

    def _guard_summary(self) -> str:
        summary = summarize_guard(self.guard, guard_operator_labels())
        return summary or tr('msg.0404')

    def _edit_guard(self) -> None:
        dialog = GuardConditionDialog(self, self.guard, lambda: self.choose_data_path('condition'))
        self.wait_window(dialog)
        if self.winfo_exists():
            self.grab_set()
            self.lift()
            self.focus_force()
        if dialog.result is not None:
            self.guard = dialog.result
            self.guard_summary.configure(text=self._guard_summary())

    def _pick(self) -> None:
        self.pick_button.config(state='disabled')
        self.pick_status.config(text='msg.0146')

        def completed(result: dict[str, str] | None, error: str | None) -> None:
            if not self.winfo_exists():
                return
            self.pick_button.config(state='normal')
            if error:
                self.pick_status.config(text=f'  {error}')
                return
            if result:
                self.values['selector_type'].set(result['selector_type'])
                self.values['selector'].set(result['selector'])
                self.values['fallback_selector_type'].set(result.get('fallback_selector_type', 'none'))
                self.values['fallback_selector'].set(result.get('fallback_selector', ''))
                if result.get('suggested_action') in ('click', 'fill'):
                    self.values['action'].set(result['suggested_action'])
                    self._update_action_fields()
                if not self.values['name'].get().strip():
                    self.values['name'].set(result['display'])
                self.pick_status.config(text=f'msg.0147{result['display']}msg.0148')
        self.pick_element(self.values['target_url'].get().strip(), completed)

    def _open_debug(self) -> None:
        self.open_debug_button.configure(state='disabled')
        self.pick_status.configure(text='msg.0357')

        def completed(error: str | None) -> None:
            if self.winfo_exists():
                self.open_debug_button.configure(state='normal')
                self.pick_status.configure(text=f'msg.0358{error}' if error else 'msg.0360')
        self.open_debug(self.values['target_url'].get().strip(), completed)

    def _close_debug(self) -> None:
        self.close_debug_button.configure(state='disabled')

        def completed(error: str | None) -> None:
            if self.winfo_exists():
                self.close_debug_button.configure(state='normal')
                self.pick_status.configure(text=f'msg.0358{error}' if error else 'msg.0361')
        self.close_debug(completed)

    def _execute_to_event(self) -> None:
        if self.execute_to_event is None:
            return
        self.execute_to_event_button.configure(state='disabled')
        self.pick_status.configure(text='msg.0362')

        def completed(error: str | None) -> None:
            if self.winfo_exists():
                self.execute_to_event_button.configure(state='normal')
                self.pick_status.configure(text=f'msg.0358{error}' if error else 'msg.0363')
        self.execute_to_event(self.values['target_url'].get().strip(), completed)

    def _choose_data(self) -> None:
        action = self.values['action'].get()
        if action == 'loop_end':
            messagebox.showinfo('msg.0149', 'msg.0150', parent=self)
            return
        path = self.choose_data_path(action)
        if path:
            self.values['data_path'].set(path)

    def _test(self) -> None:
        selector_type = self.values['selector_type'].get()
        selector = self.values['selector'].get().strip()
        if selector_type == 'none' or not selector:
            messagebox.showinfo('msg.0151', 'msg.0152', parent=self)
            return
        self.test_button.config(state='disabled')
        self.pick_status.config(text='msg.0153')

        def completed(count: int | None, error: str | None) -> None:
            if not self.winfo_exists():
                return
            self.test_button.config(state='normal')
            if error:
                self.pick_status.config(text=f'msg.0154{error}')
            elif count == 1:
                self.pick_status.config(text='msg.0155')
            elif count == 0:
                self.pick_status.config(text='msg.0156')
            else:
                self.pick_status.config(text=f'msg.0157{count}msg.0158')
        self.test_element(self.values['target_url'].get().strip(), selector_type, selector, completed)

    def _save(self) -> None:
        # 画面を閉じる前に操作固有の値を検証し、不要な項目は初期化する。
        name = self.values['name'].get().strip()
        if not name:
            messagebox.showerror('msg.0159', 'msg.0160', parent=self)
            return
        try:
            timeout = int(self.values['timeout_ms'].get())
            if timeout <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror('msg.0159', 'msg.0161', parent=self)
            return
        if self.values['action'].get() == 'retry_start':
            try:
                retry_count = int(self.values['value'].get().strip())
                if retry_count < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror('msg.0159', 'msg.0162', parent=self)
                return
        failure_choice = next((key for key, label in self.failure_action_labels.items() if label == self.values['failure_action'].get()), 'stop')
        failure_action = failure_choice if failure_choice in {'refresh', 'goto'} else 'none'
        continue_on_error = int(failure_choice != 'stop')
        failure_target = self.values['failure_target'].get().strip()
        if failure_action == 'goto' and not failure_target.startswith(('http://', 'https://')):
            messagebox.showerror('msg.0159', 'msg.0429', parent=self)
            return
        self.result = {'name': name, 'action': self.values['action'].get(), 'selector_type': self.values['selector_type'].get(), 'selector': self.values['selector'].get().strip(), 'fallback_selector_type': self.values['fallback_selector_type'].get(), 'fallback_selector': self.values['fallback_selector'].get().strip(), 'value': self.values['value'].get(), 'timeout_ms': timeout, 'enabled': int(self.values['enabled'].get()), 'continue_on_error': continue_on_error, 'failure_action': failure_action, 'failure_target': failure_target if failure_action == 'goto' else '', 'data_path': self.values['data_path'].get(), 'guard': self.guard}
        action = self.result['action']
        locator_actions = {'click', 'fill', 'select', 'wait', 'press', 'get_text', 'upload_file'}
        value_actions = {'goto', 'fill', 'select', 'press', 'get_text', 'screenshot', 'pause', 'retry_start', 'upload_file'}
        data_actions = {'fill', 'select', 'get_text', 'loop_start', 'upload_file'}
        if action not in locator_actions:
            self.result.update(selector_type='none', selector='', fallback_selector_type='none', fallback_selector='')
        if action not in value_actions:
            self.result['value'] = ''
        if action not in data_actions:
            self.result['data_path'] = ''
        if action == 'upload_file' and not self.result['value'].strip() and not self.result['data_path'].strip():
            messagebox.showerror('msg.0159', 'msg.0420', parent=self)
            self.result = None
            return
        if action in {'loop_start', 'loop_end', 'retry_start', 'retry_end'}:
            self.result['continue_on_error'] = 0
            self.result['guard'] = {'logic': 'all', 'rules': []}
        if self.result['action'] == 'get_text' and self.result['value'].strip() and (not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', self.result['value'].strip())):
            messagebox.showerror('msg.0159', 'msg.0163', parent=self)
            self.result = None
            return
        if self.result['action'] == 'get_text' and not (self.result['value'].strip() or self.result['data_path'].strip()):
            messagebox.showerror('msg.0159', 'msg.0163', parent=self)
            self.result = None
            return
        self.destroy()

class VariablesDialog(tk.Toplevel):

    def __init__(self, parent: tk.Misc, names: list[str]) -> None:
        super().__init__(parent)
        self.title('msg.0164')
        self.result: dict[str, str] | None = None
        self.variables: dict[str, tk.StringVar] = {}
        height = min(max(220, 100 + len(names) * 38), max(300, self.winfo_screenheight() - 140))
        self.geometry(f'560x{height}')
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        canvas = tk.Canvas(body, highlightthickness=0, bg='#F3F3F3')
        scrollbar = AutoScrollbar(body, orient='vertical', command=canvas.yview)
        form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=form, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        form.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        scroll = lambda event: canvas.yview_scroll(int(-event.delta / 120), 'units')
        canvas.bind('<MouseWheel>', scroll)
        form.bind('<MouseWheel>', scroll)
        ttk.Label(form, text='msg.0165').grid(row=0, column=0, columnspan=2, padx=12, pady=10, sticky='w')
        for row, name in enumerate(names, 1):
            ttk.Label(form, text=name).grid(row=row, column=0, padx=12, pady=5, sticky='e')
            variable = tk.StringVar()
            self.variables[name] = variable
            entry = ttk.Entry(form, textvariable=variable, width=40)
            entry.grid(row=row, column=1, padx=12, pady=5, sticky='ew')
            entry.bind('<MouseWheel>', scroll)
        form.columnconfigure(1, weight=1)
        ttk.Button(self, text='msg.0166', command=self._submit, style='Primary.TButton').pack(pady=10)
        self.transient(parent)
        self.grab_set()

    def _submit(self) -> None:
        self.result = {name: variable.get() for name, variable in self.variables.items()}
        self.destroy()
